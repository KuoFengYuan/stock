"""
台股資料同步腳本
策略：
  - 每日價格：TWSE + TPEX 官方 API（當日收盤即可取得，無延遲）
  - 歷史價格初始化：yfinance（僅第一次全量）
  - 財務報表：yfinance（每季更新）

用法：
  python ml/sync.py prices        # 同步當日 + 歷史價格
  python ml/sync.py financials    # 同步財務報表
  python ml/sync.py all           # 全部同步
"""
import sys
import sqlite3
import time
import json
from datetime import datetime, timedelta, date
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import yfinance as yf

DB_PATH = Path(__file__).parent.parent / "data" / "stock.db"

from stock_list import ALL_STOCKS as _ALL_STOCKS_RAW, _DYNAMIC_NAMES
from tw_names import TW_NAMES

# 只保留上市股票（.TW），排除上櫃（.TWO）
ALL_STOCKS = [s for s in _ALL_STOCKS_RAW if s.endswith(".TW")]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})
SESSION.verify = False
requests.packages.urllib3.disable_warnings()

# symbol -> 純數字代碼對照
def _code(symbol: str) -> str:
    if symbol.endswith(".TW"):
        return symbol[:-3]   # '2330.TW' -> '2330'
    return symbol

TSE_SYMBOLS = {_code(s): s for s in ALL_STOCKS}
OTC_SYMBOLS = {}  # 不再同步上櫃


def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_tables(conn)
    return conn


def _init_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stocks (
            symbol TEXT PRIMARY KEY, name TEXT NOT NULL, market TEXT NOT NULL,
            industry TEXT, listed_date TEXT, updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stock_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
            date TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
            low REAL NOT NULL, close REAL NOT NULL, volume INTEGER NOT NULL,
            adj_close REAL, UNIQUE(symbol, date)
        );
        CREATE INDEX IF NOT EXISTS idx_prices_date ON stock_prices(date);
        CREATE TABLE IF NOT EXISTS financials (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
            year INTEGER NOT NULL, quarter INTEGER NOT NULL,
            revenue REAL, operating_profit REAL, net_income REAL,
            eps REAL, equity REAL, total_assets REAL, total_debt REAL,
            UNIQUE(symbol, year, quarter)
        );
        CREATE TABLE IF NOT EXISTS institutional (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, date TEXT NOT NULL,
            foreign_net INTEGER, trust_net INTEGER, dealer_net INTEGER, total_net INTEGER,
            UNIQUE(symbol, date)
        );
        CREATE INDEX IF NOT EXISTS idx_inst_symbol_date ON institutional(symbol, date);

        CREATE TABLE IF NOT EXISTS margin_trading (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, date TEXT NOT NULL,
            margin_buy INTEGER, margin_sell INTEGER, margin_balance INTEGER,
            short_buy INTEGER, short_sell INTEGER, short_balance INTEGER,
            UNIQUE(symbol, date)
        );
        CREATE INDEX IF NOT EXISTS idx_margin_symbol_date ON margin_trading(symbol, date);

        CREATE TABLE IF NOT EXISTS recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
            date TEXT NOT NULL, score REAL NOT NULL, signal TEXT NOT NULL,
            features_json TEXT, reasons_json TEXT, model_version TEXT,
            created_at INTEGER NOT NULL, UNIQUE(symbol, date)
        );
        CREATE INDEX IF NOT EXISTS idx_rec_date_score ON recommendations(date, score DESC);
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
            status TEXT NOT NULL, records_count INTEGER, error_message TEXT,
            started_at INTEGER NOT NULL, finished_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS monthly_revenue (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
            year INTEGER NOT NULL, month INTEGER NOT NULL,
            revenue REAL NOT NULL, yoy REAL, mom REAL,
            UNIQUE(symbol, year, month)
        );
        CREATE INDEX IF NOT EXISTS idx_monthly_rev_symbol ON monthly_revenue(symbol, year DESC, month DESC);
    """)


def log_sync(conn, sync_type, status, records_count=None, error=None, started_at=None):
    if started_at is None:
        started_at = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO sync_log (type, status, records_count, error_message, started_at, finished_at) VALUES (?,?,?,?,?,?)",
        (sync_type, status, records_count, error, started_at, int(time.time() * 1000))
    )
    conn.commit()


def sync_stock_list(conn):
    now = int(time.time() * 1000)
    count = 0
    for symbol in ALL_STOCKS:
        # 優先：TW_NAMES 中文名 > 動態抓到的名稱 > symbol 本身
        name = TW_NAMES.get(symbol) or _DYNAMIC_NAMES.get(symbol) or symbol
        market = "OTC" if symbol.endswith(".TWO") else "TSE"
        conn.execute(
            "INSERT OR REPLACE INTO stocks (symbol, name, market, updated_at) VALUES (?,?,?,?)",
            (symbol, name, market, now)
        )
        count += 1

    # 清除不在清單中的股票及其關聯資料
    placeholders = ",".join("?" * len(ALL_STOCKS))
    for table in ("stock_prices", "institutional", "margin_trading", "financials", "monthly_revenue", "recommendations"):
        cur = conn.execute(
            f"DELETE FROM {table} WHERE symbol NOT IN ({placeholders})", ALL_STOCKS
        )
        if cur.rowcount:
            print(f"  purge {table}: {cur.rowcount} rows", flush=True)
    cur = conn.execute(f"DELETE FROM stocks WHERE symbol NOT IN ({placeholders})", ALL_STOCKS)
    if cur.rowcount:
        print(f"  purge stocks: {cur.rowcount} rows", flush=True)

    conn.commit()
    print(f"股票清單：{count} 檔", flush=True)
    return count


# ── TWSE / TPEX 即時 API ──────────────────────────────────────

def _twse_date_str(d: date) -> str:
    """轉成 TWSE 日期格式 YYYYMMDD"""
    return d.strftime("%Y%m%d")

def _tpex_date_str(d: date) -> str:
    """轉成 TPEX 民國年格式 YYY/MM/DD"""
    roc_year = d.year - 1911
    return f"{roc_year}/{d.month:02d}/{d.day:02d}"


def fetch_twse_day(target_date: date) -> dict[str, dict]:
    """抓 TWSE 某日全部上市股票收盤資料，回傳 {code: {open,high,low,close,volume}}"""
    url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=json&date={_twse_date_str(target_date)}"
    try:
        r = SESSION.get(url, timeout=15)
        data = r.json()
        if data.get("stat") != "OK":
            return {}
        # fields: 證券代號,證券名稱,成交股數,成交金額,開盤價,最高價,最低價,收盤價,漲跌價差,成交筆數
        result = {}
        for row in data.get("data", []):
            code = row[0].strip()
            if code not in TSE_SYMBOLS:
                continue
            try:
                def _p(s): return float(s.replace(",", "")) if s not in ("--", "") else None
                o, h, l, c = _p(row[4]), _p(row[5]), _p(row[6]), _p(row[7])
                vol = int(row[2].replace(",", "")) // 1000  # 股 -> 張
                if c is None:
                    continue
                result[code] = {"open": o or c, "high": h or c, "low": l or c, "close": c, "volume": vol}
            except Exception:
                pass
        return result
    except Exception as e:
        print(f"  [TWSE ERROR] {e}", flush=True)
        return {}


def fetch_tpex_day(target_date: date) -> dict[str, dict]:
    """抓 TPEX 某日全部上櫃股票收盤資料"""
    url = f"https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php?l=zh-tw&o=json&d={_tpex_date_str(target_date)}"
    try:
        r = SESSION.get(url, timeout=15)
        data = r.json()
        tables = data.get("tables", [])
        if not tables:
            return {}
        result = {}
        for row in tables[0].get("data", []):
            code = row[0].strip()
            if code not in OTC_SYMBOLS:
                continue
            try:
                def _p(s): return float(s.replace(",", "")) if s not in ("--", "", "---") else None
                c = _p(row[2])   # 收盤
                o = _p(row[4])   # 開盤
                h = _p(row[5])   # 最高
                l = _p(row[6])   # 最低
                vol_str = row[8] if len(row) > 8 else ""
                vol = int(vol_str.replace(",", "")) // 1000 if vol_str.replace(",", "").isdigit() else 0
                if c is None:
                    continue
                result[code] = {"open": o or c, "high": h or c, "low": l or c, "close": c, "volume": vol}
            except Exception:
                pass
        return result
    except Exception as e:
        print(f"  [TPEX ERROR] {e}", flush=True)
        return {}


def _insert_day(conn, symbol: str, date_str: str, p: dict) -> bool:
    try:
        conn.execute(
            "INSERT OR REPLACE INTO stock_prices (symbol, date, open, high, low, close, volume, adj_close) VALUES (?,?,?,?,?,?,?,?)",
            (symbol, date_str, p["open"], p["high"], p["low"], p["close"], p["volume"], p["close"])
        )
        return True
    except Exception:
        return False


def sync_prices(conn):
    """
    同步策略：
    1. 若 DB 無資料 → 用 yfinance 初始化近2年歷史
    2. 找出尚缺的交易日（DB 最新日期到今天）→ 用 TWSE+TPEX 補齊
    """
    started_at = int(time.time() * 1000)
    total = 0

    row = conn.execute("SELECT MAX(date) FROM stock_prices").fetchone()
    latest_in_db = row[0] if row and row[0] else None

    # 計算有足夠資料（>=120筆）的股票數
    covered = conn.execute(
        "SELECT COUNT(*) FROM (SELECT symbol, COUNT(*) as c FROM stock_prices GROUP BY symbol HAVING c >= 120)"
    ).fetchone()[0]
    need_bulk = not latest_in_db or covered < len(ALL_STOCKS) * 0.5  # 覆蓋率不足50%就補

    # ── 步驟1 + 步驟2：用 TWSE+TPEX 官方 API 補齊所有缺少的日期（歷史 + 近期）──
    if need_bulk:
        print(f"歷史資料不足（{covered}/{len(ALL_STOCKS)} 檔），用 TWSE+TPEX 官方 API 補齊近2年...", flush=True)
        hist_start = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
    elif latest_in_db:
        hist_start = latest_in_db  # 從最後日期往後補
    else:
        hist_start = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")

    total += _twse_tpex_history_bulk(conn, hist_start)
    row = conn.execute("SELECT MAX(date) FROM stock_prices").fetchone()
    latest_in_db = row[0] if row and row[0] else None

    if not need_bulk and latest_in_db:
        # 已是最新
        pass

    log_sync(conn, "prices", "success", total, started_at=started_at)
    print(f"價格同步完成，共新增 {total} 筆", flush=True)
    return total


def _fetch_stock_month(code: str, is_otc: bool, ym: str) -> list:
    """抓單一股票某月的日資料（TWSE 個股日 K API）"""
    rows = []
    try:
        if not is_otc:
            url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={ym}01&stockNo={code}"
            r = SESSION.get(url, timeout=10)
            data = r.json()
            if data.get("stat") != "OK":
                return rows
            # fields: 日期(民國),成交股數,成交金額,開盤價,最高價,最低價,收盤價,漲跌價差,成交筆數
            year_base = int(ym[:4])
            for row in data.get("data", []):
                try:
                    date_parts = row[0].split("/")
                    y = int(date_parts[0]) + 1911
                    m, d = int(date_parts[1]), int(date_parts[2])
                    def _p(s): return float(s.replace(",", "")) if s not in ("--", "", "X") else None
                    o, h, l, c = _p(row[3]), _p(row[4]), _p(row[5]), _p(row[6])
                    if c is None:
                        continue
                    vol = int(row[1].replace(",", "")) // 1000
                    rows.append((f"{y:04d}-{m:02d}-{d:02d}", o or c, h or c, l or c, c, vol))
                except Exception:
                    pass
        else:
            # TPEX 個股月 API
            roc_year = int(ym[:4]) - 1911
            roc_month = int(ym[4:])
            url = f"https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_result.php?l=zh-tw&o=json&d={roc_year}/{roc_month:02d}/01&stkno={code}"
            r = SESSION.get(url, timeout=10)
            data = r.json()
            # fields: 日期,成交股數,成交金額,開盤,最高,最低,收盤,...
            for row in data.get("aaData", []):
                try:
                    date_parts = row[0].split("/")
                    y = int(date_parts[0]) + 1911
                    m, d_val = int(date_parts[1]), int(date_parts[2])
                    def _p(s): return float(s.replace(",", "")) if s not in ("--", "", "X", "---") else None
                    o, h, l, c = _p(row[3]), _p(row[4]), _p(row[5]), _p(row[6])
                    if c is None:
                        continue
                    vol = int(row[1].replace(",", "")) // 1000 if row[1].replace(",", "").isdigit() else 0
                    rows.append((f"{y:04d}-{m:02d}-{d_val:02d}", o or c, h or c, l or c, c, vol))
                except Exception:
                    pass
    except Exception:
        pass
    return rows


def _twse_tpex_history_bulk(conn, start_date: str) -> int:
    """
    用 TWSE + TPEX 個股月日 K API 並發補齊全市場歷史價格
    """
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    today = date.today()

    # 統計各 (symbol, YYYY-MM) 已有的交易日數，>=15 天視為完整，跳過重下
    month_counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT symbol, substr(date,1,7) as ym, COUNT(*) as cnt FROM stock_prices GROUP BY symbol, ym"
    ).fetchall():
        month_counts[f"{row[0]}_{row[1]}"] = row[2]

    # 建立要抓的 (symbol, ym) 任務清單
    months = []
    cur = date(start.year, start.month, 1)
    while cur <= today:
        months.append(cur.strftime("%Y%m"))
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)

    last_ym = months[-1]
    tasks = []
    for symbol in ALL_STOCKS:
        code = _code(symbol)
        is_otc = symbol.endswith(".TWO")
        for ym in months:
            key = f"{symbol}_{ym[:4]}-{ym[4:]}"
            # 最新月永遠重抓（可能有新交易日）；其他月有 >=15 天才跳過
            if ym != last_ym and month_counts.get(key, 0) >= 15:
                continue
            tasks.append((symbol, code, is_otc, ym))

    if not tasks:
        print("  無需補齊歷史資料", flush=True)
        return 0

    print(f"  共 {len(tasks)} 個下載任務（{len(ALL_STOCKS)} 檔 × {len(months)} 個月）...", flush=True)
    total = 0
    batch_results = []

    def _dl(task):
        symbol, code, is_otc, ym = task
        day_rows = _fetch_stock_month(code, is_otc, ym)
        return symbol, day_rows

    WORKERS = 120
    BATCH_SIZE = 2000
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(_dl, t): t for t in tasks}
        for fut in as_completed(futures):
            symbol, day_rows = fut.result()
            for date_str, o, h, l, c, vol in day_rows:
                batch_results.append((symbol, date_str, o, h, l, c, vol, c))
            done += 1
            if done % 200 == 0 or done == len(tasks):
                pct = done * 100 // len(tasks)
                print(f"  下載進度 {done}/{len(tasks)} ({pct}%)，暫存 {len(batch_results)} 筆", flush=True)
            # 每累積 BATCH_SIZE 筆就先寫入，避免記憶體暴增
            if len(batch_results) >= BATCH_SIZE:
                conn.executemany(
                    "INSERT OR REPLACE INTO stock_prices (symbol, date, open, high, low, close, volume, adj_close) VALUES (?,?,?,?,?,?,?,?)",
                    batch_results
                )
                conn.commit()
                total += len(batch_results)
                batch_results = []

    # 寫入剩餘資料
    if batch_results:
        conn.executemany(
            "INSERT OR REPLACE INTO stock_prices (symbol, date, open, high, low, close, volume, adj_close) VALUES (?,?,?,?,?,?,?,?)",
            batch_results
        )
        conn.commit()
        total += len(batch_results)

    print(f"  TWSE+TPEX 個股歷史資料：{total} 筆", flush=True)
    return total


def _yf_insert(conn, symbol, df) -> int:
    count = 0
    for d, row in df.iterrows():
        try:
            conn.execute(
                "INSERT OR REPLACE INTO stock_prices (symbol, date, open, high, low, close, volume, adj_close) VALUES (?,?,?,?,?,?,?,?)",
                (symbol, d.strftime("%Y-%m-%d"),
                 float(row.get("Open", 0) or 0), float(row.get("High", 0) or 0),
                 float(row.get("Low", 0) or 0), float(row.get("Close", 0) or 0),
                 int(row.get("Volume", 0) or 0), float(row.get("Close", 0) or 0))
            )
            count += 1
        except Exception:
            pass
    return count


def sync_financials(conn):
    """同步財務報表（yfinance，並發）"""
    started_at = int(time.time() * 1000)
    total = 0

    existing = {}
    for row in conn.execute("SELECT symbol, MAX(year*10+quarter) as latest FROM financials GROUP BY symbol").fetchall():
        existing[row[0]] = row[1]
    now_yq = datetime.now().year * 10 + (datetime.now().month - 1) // 3 + 1

    need_dl = [s for s in ALL_STOCKS if existing.get(s, 0) < now_yq]
    print(f"同步財務報表，{len(need_dl)} 檔需更新...", flush=True)

    def _fetch_fin(symbol):
        try:
            ticker = yf.Ticker(symbol)
            income = ticker.quarterly_income_stmt
            balance = ticker.quarterly_balance_sheet
            if income is None or income.empty:
                return symbol, []
            rows = []
            for col in income.columns:
                try:
                    year, month = col.year, col.month
                    quarter = (month - 1) // 3 + 1
                    revenue = _get_val(income, col, ["Total Revenue", "Revenue"])
                    op_profit = _get_val(income, col, ["Operating Income", "EBIT"])
                    net_income = _get_val(income, col, ["Net Income"])
                    eps = _get_val(income, col, ["Basic EPS", "Diluted EPS"])
                    equity = _get_val(balance, col, ["Stockholders Equity", "Total Equity Gross Minority Interest"]) if balance is not None and not balance.empty else None
                    total_assets = _get_val(balance, col, ["Total Assets"]) if balance is not None and not balance.empty else None
                    total_debt = _get_val(balance, col, ["Total Debt", "Long Term Debt"]) if balance is not None and not balance.empty else None
                    rows.append((symbol, year, quarter, revenue, op_profit, net_income, eps, equity, total_assets, total_debt))
                except Exception:
                    pass
            return symbol, rows
        except Exception as e:
            return symbol, []

    done = 0
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(_fetch_fin, s): s for s in need_dl}
        for fut in as_completed(futures):
            symbol, rows = fut.result()
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO financials (symbol, year, quarter, revenue, operating_profit, net_income, eps, equity, total_assets, total_debt) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    rows
                )
                conn.commit()
                total += len(rows)
            done += 1
            if done % 100 == 0:
                print(f"  財務進度 {done}/{len(need_dl)}，累計 {total} 筆", flush=True)

    log_sync(conn, "financials", "success", total, started_at=started_at)
    print(f"財務報表同步完成，共 {total} 筆", flush=True)
    return total


def _get_val(df, col, keys):
    for k in keys:
        if k in df.index:
            v = df.loc[k, col]
            if pd.notna(v):
                return float(v)
    return None


# ── 三大法人 ──────────────────────────────────────────────────

def fetch_twse_institutional(target_date: date) -> dict[str, dict]:
    """TWSE 三大法人買賣超（外資、投信、自營商）"""
    url = f"https://www.twse.com.tw/fund/T86?response=json&date={_twse_date_str(target_date)}&selectType=ALLBUT0999"
    try:
        r = SESSION.get(url, timeout=15)
        data = r.json()
        if data.get("stat") != "OK":
            return {}
        # 19 欄位順序（實測）：
        # [0]代號 [1]名稱
        # [2-4]  外資(不含陸資) 買/賣/淨
        # [5-7]  外資(含陸資)   買/賣/淨
        # [8-10] 投信           買/賣/淨
        # [11]   自營合計淨買
        # [12-14] 自營(自行)    買/賣/淨
        # [15-17] 自營(避險)    買/賣/淨
        # [18]   三大法人合計淨買
        result = {}
        for row in data.get("data", []):
            code = row[0].strip()
            if code not in TSE_SYMBOLS:
                continue
            try:
                def _n(s): return int(s.replace(",", "").replace(" ", "") or 0)
                result[code] = {
                    "foreign_net": _n(row[4]),
                    "trust_net":   _n(row[10]),
                    "dealer_net":  _n(row[11]),
                    "total_net":   _n(row[18]),
                }
            except Exception:
                pass
        return result
    except Exception as e:
        print(f"  [TWSE 三大法人 ERROR] {e}", flush=True)
        return {}


def fetch_tpex_institutional(target_date: date) -> dict[str, dict]:
    """TPEX 三大法人買賣超"""
    url = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
    try:
        r = SESSION.post(url, data={"l": "zh-tw", "o": "json", "se": "EW", "t": "D", "d": _tpex_date_str(target_date)}, timeout=15)
        import json as _json
        data = _json.loads(r.content.decode("utf-8"))
        tables = data.get("tables", [])
        if not tables:
            return {}
        # 24 欄位順序（實測）：
        # [0]代號 [1]名稱
        # [2-4]  外資(不含陸資) 買/賣/淨
        # [5-7]  外資(含陸資)   買/賣/淨
        # [8-10] 外資合計       買/賣/淨
        # [11-13] 自營(自行)    買/賣/淨
        # [14-16] 自營(避險)    買/賣/淨
        # [17-19] 投信          買/賣/淨
        # [20-22] 自營合計      買/賣/淨
        # [23]   三大法人合計淨買
        result = {}
        for row in tables[0].get("data", []):
            code = row[0].strip()
            if code not in OTC_SYMBOLS:
                continue
            try:
                def _n(s): return int(s.replace(",", "").replace(" ", "") or 0)
                result[code] = {
                    "foreign_net": _n(row[4]),
                    "trust_net":   _n(row[19]),
                    "dealer_net":  _n(row[22]),
                    "total_net":   _n(row[23]),
                }
            except Exception:
                pass
        return result
    except Exception as e:
        print(f"  [TPEX 三大法人 ERROR] {e}", flush=True)
        return {}


def fetch_twse_margin(target_date: date) -> dict[str, dict]:
    """TWSE 個股融資融券餘額"""
    url = f"https://www.twse.com.tw/exchangeReport/MI_MARGN?response=json&date={_twse_date_str(target_date)}&selectType=ALL"
    try:
        r = SESSION.get(url, timeout=15)
        data = r.json()
        tables = data.get("tables", [])
        # table[1] = 個股明細
        if len(tables) < 2:
            return {}
        # fields: 代號,名稱, 融資買進,融資賣出,融資現償,融資餘額,融資前日餘額, 融券買進,融券賣出,融券現券,融券餘額,融券前日餘額, ...
        result = {}
        for row in tables[1].get("data", []):
            code = row[0].strip()
            if code not in TSE_SYMBOLS:
                continue
            try:
                def _n(s): return int(s.replace(",", "").replace(" ", "") or 0)
                result[code] = {
                    "margin_buy":     _n(row[2]),
                    "margin_sell":    _n(row[3]),
                    "margin_balance": _n(row[5]),
                    "short_buy":      _n(row[7]),
                    "short_sell":     _n(row[8]),
                    "short_balance":  _n(row[10]),
                }
            except Exception:
                pass
        return result
    except Exception as e:
        print(f"  [TWSE 融資券 ERROR] {e}", flush=True)
        return {}


def sync_chips(conn):
    """同步三大法人 + 融資融券（增量，僅上市）"""
    started_at = int(time.time() * 1000)

    today = date.today()

    # 對齊價格資料的最早日期（TWSE 法人 API 最多約 2 年）
    price_min_row = conn.execute("SELECT MIN(date) FROM stock_prices").fetchone()
    price_start_raw = datetime.strptime(price_min_row[0], "%Y-%m-%d").date() if price_min_row and price_min_row[0] else today - timedelta(days=270)
    earliest_allowed = today - timedelta(days=730)  # 最多回填 2 年
    price_start = max(price_start_raw, earliest_allowed)

    inst_min_row = conn.execute("SELECT MIN(date) FROM institutional").fetchone()
    inst_start = datetime.strptime(inst_min_row[0], "%Y-%m-%d").date() if inst_min_row and inst_min_row[0] else None

    row = conn.execute("SELECT MAX(date) FROM institutional").fetchone()
    latest_in_db = row[0] if row and row[0] else None

    # 需要補的日期：前面的缺口 + 後面的增量
    dates_to_fill = []
    if inst_start and price_start < inst_start:
        # 回填缺口
        gap_dates = [price_start + timedelta(days=i) for i in range((inst_start - price_start).days)]
        dates_to_fill += [d for d in gap_dates if d.weekday() < 5]

    if latest_in_db:
        start = datetime.strptime(latest_in_db, "%Y-%m-%d").date()
        new_dates = [start + timedelta(days=i) for i in range(1, (today - start).days + 1)]
        dates_to_fill += [d for d in new_dates if d.weekday() < 5]
    else:
        all_dates = [price_start + timedelta(days=i) for i in range((today - price_start).days + 1)]
        dates_to_fill = [d for d in all_dates if d.weekday() < 5]

    if not dates_to_fill:
        print("籌碼資料已是最新", flush=True)
        return 0

    print(f"同步籌碼資料，{len(dates_to_fill)} 個交易日...", flush=True)
    inst_total = 0
    margin_total = 0

    for d in dates_to_fill:
        date_str = d.strftime("%Y-%m-%d")

        # 三大法人（僅上市）
        tse_inst = fetch_twse_institutional(d)
        day_inst = 0

        for code, v in tse_inst.items():
            symbol = TSE_SYMBOLS.get(code)
            if not symbol:
                continue
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO institutional (symbol, date, foreign_net, trust_net, dealer_net, total_net) VALUES (?,?,?,?,?,?)",
                    (symbol, date_str, v["foreign_net"], v["trust_net"], v["dealer_net"], v["total_net"])
                )
                day_inst += 1
            except Exception:
                pass

        # 融資融券（只有上市）
        margin = fetch_twse_margin(d)
        day_margin = 0
        for code, v in margin.items():
            symbol = TSE_SYMBOLS.get(code)
            if not symbol:
                continue
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO margin_trading (symbol, date, margin_buy, margin_sell, margin_balance, short_buy, short_sell, short_balance) VALUES (?,?,?,?,?,?,?,?)",
                    (symbol, date_str, v["margin_buy"], v["margin_sell"], v["margin_balance"], v["short_buy"], v["short_sell"], v["short_balance"])
                )
                day_margin += 1
            except Exception:
                pass

        conn.commit()
        if day_inst > 0 or day_margin > 0:
            print(f"  {date_str}: 法人 {day_inst} 檔, 融資券 {day_margin} 檔", flush=True)
        else:
            print(f"  {date_str}: 休市或無資料", flush=True)

        inst_total += day_inst
        margin_total += day_margin

    log_sync(conn, "chips", "success", inst_total + margin_total, started_at=started_at)
    print(f"籌碼同步完成：法人 {inst_total} 筆，融資券 {margin_total} 筆", flush=True)
    return inst_total + margin_total


def _fetch_monthly_revenue_finmind(code: str, start_date: str) -> list[dict]:
    """用 FinMind API 抓單一股票月營收。回傳 [{year, month, revenue}, ...]"""
    try:
        r = SESSION.get(
            f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockMonthRevenue&data_id={code}&start_date={start_date}",
            timeout=10
        )
        j = r.json()
        if j.get("status") != 200:
            return []
        result = []
        for d in j.get("data", []):
            rev = d.get("revenue")
            if rev and rev > 0:
                result.append({
                    "year": d["revenue_year"],
                    "month": d["revenue_month"],
                    "revenue": float(rev),  # 單位：元
                })
        return result
    except Exception:
        return []


def sync_monthly_revenue(conn):
    """
    同步月營收（FinMind API 逐檔抓取）。
    增量更新：只抓 DB 中尚缺的資料。首次補近30個月（含去年同期，用於算 YoY）。
    """
    started_at = int(time.time() * 1000)

    row = conn.execute("SELECT year, month FROM monthly_revenue ORDER BY year DESC, month DESC LIMIT 1").fetchone()
    if row:
        # 增量：從最新月開始抓（重抓最新月以防更新）
        start_date = f"{row[0]}-{row[1]:02d}-01"
    else:
        # 首次：補近30個月（確保有去年同期可算 YoY）
        d = date.today() - timedelta(days=900)
        start_date = d.strftime("%Y-%m-%d")

    print(f"同步月營收（FinMind API），起始日 {start_date}...", flush=True)

    all_symbols = list(ALL_STOCKS)
    total = 0
    batch = []

    def _dl(symbol):
        code = _code(symbol)
        return symbol, _fetch_monthly_revenue_finmind(code, start_date)

    WORKERS = 20  # FinMind 免費版限速，不能太快
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(_dl, s): s for s in all_symbols}
        for fut in as_completed(futures):
            symbol, rows = fut.result()
            for r in rows:
                batch.append((symbol, r["year"], r["month"], r["revenue"]))
            done += 1
            if done % 200 == 0 or done == len(all_symbols):
                print(f"  下載進度 {done}/{len(all_symbols)} ({done*100//len(all_symbols)}%)，暫存 {len(batch)} 筆", flush=True)

    if not batch:
        print("月營收：無新資料", flush=True)
        log_sync(conn, "monthly_revenue", "success", 0, started_at=started_at)
        return 0

    # 先寫入原始 revenue（不含 YoY/MoM）
    for symbol, year, month, rev in batch:
        conn.execute(
            "INSERT OR REPLACE INTO monthly_revenue (symbol, year, month, revenue, yoy, mom) VALUES (?,?,?,?,NULL,NULL)",
            (symbol, year, month, rev)
        )
        total += 1
    conn.commit()
    print(f"  已寫入 {total} 筆原始營收", flush=True)

    # 批量計算 YoY / MoM
    updated = 0
    for symbol, year, month, rev in batch:
        prev_year_row = conn.execute(
            "SELECT revenue FROM monthly_revenue WHERE symbol=? AND year=? AND month=?",
            (symbol, year - 1, month)
        ).fetchone()
        yoy = (rev - prev_year_row[0]) / prev_year_row[0] * 100 if prev_year_row and prev_year_row[0] and prev_year_row[0] > 0 else None

        prev_m = month - 1 if month > 1 else 12
        prev_m_y = year if month > 1 else year - 1
        prev_month_row = conn.execute(
            "SELECT revenue FROM monthly_revenue WHERE symbol=? AND year=? AND month=?",
            (symbol, prev_m_y, prev_m)
        ).fetchone()
        mom = (rev - prev_month_row[0]) / prev_month_row[0] * 100 if prev_month_row and prev_month_row[0] and prev_month_row[0] > 0 else None

        if yoy is not None or mom is not None:
            conn.execute(
                "UPDATE monthly_revenue SET yoy=?, mom=? WHERE symbol=? AND year=? AND month=?",
                (yoy, mom, symbol, year, month)
            )
            updated += 1
    conn.commit()

    log_sync(conn, "monthly_revenue", "success", total, started_at=started_at)
    print(f"月營收同步完成，共 {total} 筆（{updated} 筆有 YoY/MoM）", flush=True)
    return total


def sync_otc_prices_yf(conn):
    """用 yfinance 補齊上櫃 (OTC) 股票缺失的價格資料"""
    import yfinance as yf

    otc_missing = conn.execute("""
        SELECT s.symbol FROM stocks s
        WHERE s.market = 'OTC'
        AND NOT EXISTS (SELECT 1 FROM stock_prices p WHERE p.symbol = s.symbol)
    """).fetchall()

    if not otc_missing:
        print("上櫃價格已完整，無需補齊", flush=True)
        return 0

    symbols = [r[0] for r in otc_missing]
    print(f"用 yfinance 補齊 {len(symbols)} 檔上櫃股票價格（近2年）...", flush=True)
    total = 0

    # 分批下載（yfinance 支援多檔同時下載）
    BATCH = 50
    for i in range(0, len(symbols), BATCH):
        batch = symbols[i:i+BATCH]
        tickers = " ".join(batch)
        try:
            df = yf.download(tickers, period="2y", group_by="ticker", progress=False, threads=True)
        except Exception as e:
            print(f"  [WARN] batch {i//BATCH+1}: {e}", flush=True)
            continue

        for sym in batch:
            try:
                if len(batch) == 1:
                    stock_df = df
                else:
                    stock_df = df[sym] if sym in df.columns.get_level_values(0) else None
                if stock_df is None or stock_df.empty:
                    continue
                stock_df = stock_df.dropna(subset=["Close"])
                count = 0
                for d, row in stock_df.iterrows():
                    dt = d[0] if isinstance(d, tuple) else d
                    try:
                        o = float(row.get("Open", 0) or 0)
                        h = float(row.get("High", 0) or 0)
                        l = float(row.get("Low", 0) or 0)
                        c = float(row.get("Close", 0) or 0)
                        v = int(row.get("Volume", 0) or 0) // 1000  # 股 -> 張
                        if c <= 0:
                            continue
                        conn.execute(
                            "INSERT OR REPLACE INTO stock_prices (symbol, date, open, high, low, close, volume, adj_close) VALUES (?,?,?,?,?,?,?,?)",
                            (sym, dt.strftime("%Y-%m-%d"), o, h, l, c, v, c)
                        )
                        count += 1
                    except Exception:
                        pass
                total += count
            except Exception:
                pass

        conn.commit()
        pct = min(100, (i + BATCH) * 100 // len(symbols))
        print(f"  進度 {min(i+BATCH, len(symbols))}/{len(symbols)} ({pct}%)，累計 {total} 筆", flush=True)

    print(f"上櫃價格補齊完成，共 {total} 筆", flush=True)
    return total


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    conn = get_conn()

    print("同步股票清單...", flush=True)
    sync_stock_list(conn)

    if mode in ("prices", "prices_chips", "all"):
        sync_prices(conn)
        sync_otc_prices_yf(conn)  # 補齊 TPEX 被 WAF 擋的上櫃股票

    if mode in ("financials", "all"):
        sync_financials(conn)

    if mode in ("chips", "prices_chips", "all"):
        sync_chips(conn)

    if mode in ("monthly_revenue", "all"):
        sync_monthly_revenue(conn)

    conn.close()

    # 同步標籤（不需要 conn，獨立操作）
    try:
        from sync_tags import sync_tags
        sync_tags()
    except Exception as e:
        print(f"[WARN] 標籤同步失敗: {e}", flush=True)

    print("完成！", flush=True)
