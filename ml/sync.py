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
from datetime import datetime, timedelta, date
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import yfinance as yf

DB_PATH = Path(__file__).parent.parent / "data" / "stock.db"


def _retry_get(session, url, timeout=30, retries=3, delay=3, label="API"):
    """帶 retry 的 GET 請求"""
    for attempt in range(retries + 1):
        try:
            r = session.get(url, timeout=timeout)
            return r
        except Exception as e:
            if attempt < retries:
                print(f"  [{label} ERROR] {e}，{delay}秒後重試({attempt+1}/{retries})...", flush=True)
                time.sleep(delay)
            else:
                print(f"  [{label} FAILED] {e}", flush=True)
                return None

from stock_list import ALL_STOCKS as _ALL_STOCKS_RAW, _DYNAMIC_NAMES
from tw_names import TW_NAMES

# 只保留上市股票（.TW），排除上櫃（.TWO）
ALL_STOCKS = [s for s in _ALL_STOCKS_RAW if s.endswith(".TW")]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})
SESSION.verify = False  # TWSE 有時憑證不完整（Missing Subject Key Identifier）
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# symbol -> 純數字代碼對照（僅上市）
def _code(symbol: str) -> str:
    return symbol[:-3]  # '2330.TW' -> '2330'

TSE_SYMBOLS = {_code(s): s for s in ALL_STOCKS}


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

        -- TAIFEX 外資期貨淨未平倉（大盤方向領先指標，單位：口）
        CREATE TABLE IF NOT EXISTS futures_positions (
            date TEXT PRIMARY KEY,
            foreign_long_oi INTEGER,
            foreign_short_oi INTEGER,
            foreign_net_oi INTEGER
        );
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
    for table in ("stock_prices", "institutional", "margin_trading", "financials", "monthly_revenue", "recommendations", "stock_tags"):
        cur = conn.execute(
            f"DELETE FROM {table} WHERE symbol NOT IN ({placeholders})", ALL_STOCKS
        )
        if cur.rowcount:
            print(f"  purge {table}: {cur.rowcount} rows", flush=True)
    cur = conn.execute(f"DELETE FROM stocks WHERE symbol NOT IN ({placeholders})", ALL_STOCKS)
    if cur.rowcount:
        print(f"  purge stocks: {cur.rowcount} rows", flush=True)

    # 清除損壞資料（close=0 或 NULL 的價格列）
    cur = conn.execute("DELETE FROM stock_prices WHERE close IS NULL OR close <= 0")
    if cur.rowcount:
        print(f"  purge 損壞價格: {cur.rowcount} rows", flush=True)

    # 清除超過 2 年的價格/籌碼（保留訓練用的時間窗）
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=730)).strftime("%Y-%m-%d")
    for table in ("stock_prices", "institutional", "margin_trading"):
        cur = conn.execute(f"DELETE FROM {table} WHERE date < ?", (cutoff,))
        if cur.rowcount:
            print(f"  purge {table} 超過 2 年: {cur.rowcount} rows", flush=True)

    # 清除超過 3 年的月營收（保留 YoY 計算需要的去年同期）
    cutoff_rev = date.today().year - 3
    cur = conn.execute("DELETE FROM monthly_revenue WHERE year < ?", (cutoff_rev,))
    if cur.rowcount:
        print(f"  purge monthly_revenue 超過 3 年: {cur.rowcount} rows", flush=True)

    # 清除超過 1 年的舊推薦（每日一筆，保留最近讓前端能顯示歷史）
    cutoff_rec = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")
    cur = conn.execute("DELETE FROM recommendations WHERE date < ?", (cutoff_rec,))
    if cur.rowcount:
        print(f"  purge recommendations 超過 1 年: {cur.rowcount} rows", flush=True)

    # sync_log 只保留最近 500 筆
    cur = conn.execute(
        "DELETE FROM sync_log WHERE id NOT IN (SELECT id FROM sync_log ORDER BY id DESC LIMIT 500)"
    )
    if cur.rowcount:
        print(f"  purge sync_log: {cur.rowcount} rows", flush=True)

    conn.commit()

    # VACUUM 很貴（重寫整個 DB 檔），只在 DB 大於 1.5GB 時才跑
    import os
    try:
        db_size = os.path.getsize(DB_PATH)
        if db_size > 1_500_000_000:
            print(f"  DB 大小 {db_size/1e9:.2f}GB > 1.5GB，執行 VACUUM 回收空間...", flush=True)
            conn.execute("VACUUM")
    except Exception:
        pass

    print(f"股票清單：{count} 檔", flush=True)
    return count


# ── TWSE / TPEX 即時 API ──────────────────────────────────────

def _twse_date_str(d: date) -> str:
    """轉成 TWSE 日期格式 YYYYMMDD"""
    return d.strftime("%Y%m%d")

def fetch_twse_day(target_date: date) -> tuple[str | None, dict[str, dict]]:
    """抓 TWSE 當日全部上市股票收盤資料。
    注意：STOCK_DAY_ALL 只回傳「最新交易日」，不論 date 參數；
    實際日期以 API 回傳的 data.date 為準，避免把舊日期覆蓋成新資料。
    回傳 (實際日期 YYYY-MM-DD, {code: {open,high,low,close,volume}})
    """
    url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=json&date={_twse_date_str(target_date)}"
    r = _retry_get(SESSION, url, label="TWSE 日K")
    if not r:
        return None, {}
    try:
        data = r.json()
        if data.get("stat") != "OK":
            return None, {}
        raw_date = data.get("date")  # YYYYMMDD
        actual_date = None
        if raw_date and len(raw_date) == 8:
            actual_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
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
        return actual_date, result
    except Exception as e:
        print(f"  [TWSE 日K parse ERROR] {e}", flush=True)
        return None, {}


def fetch_twse_mi_index(target_date: date) -> tuple[str | None, dict[str, dict]]:
    """抓 TWSE MI_INDEX 指定日全市場日K資料（可抓歷史日期）。
    比 STOCK_DAY_ALL 強，可傳指定日期抓歷史資料。
    假日會回傳空資料。
    回傳 (實際日期 YYYY-MM-DD, {code: {open,high,low,close,volume}})
    """
    url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={_twse_date_str(target_date)}&type=ALL"
    r = _retry_get(SESSION, url, label=f"TWSE MI {target_date}")
    if not r:
        return None, {}
    try:
        data = r.json()
        if data.get("stat") != "OK":
            return None, {}
        raw_date = data.get("date")
        actual_date = None
        if raw_date and len(raw_date) == 8:
            actual_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
        tables = data.get("tables", [])
        # Table 8 是「每日收盤行情（全部）」
        # fields: [證券代號, 證券名稱, 成交股數, 成交筆數, 成交金額, 開盤價, 最高價, 最低價, 收盤價, ...]
        target_table = None
        for t in tables:
            fields = t.get("fields", [])
            if len(fields) >= 9 and "證券代號" in fields[0] and "開盤" in str(fields[5]):
                target_table = t
                break
        if not target_table:
            return actual_date, {}
        result = {}
        for row in target_table.get("data", []):
            code = row[0].strip()
            if code not in TSE_SYMBOLS:
                continue
            try:
                def _p(s):
                    s = s.strip() if isinstance(s, str) else s
                    return float(s.replace(",", "")) if s not in ("--", "", "---") else None
                vol_str = row[2].replace(",", "")
                vol = int(vol_str) // 1000 if vol_str.isdigit() else 0
                o, h, l, c = _p(row[5]), _p(row[6]), _p(row[7]), _p(row[8])
                if c is None:
                    continue
                result[code] = {"open": o or c, "high": h or c, "low": l or c, "close": c, "volume": vol}
            except Exception:
                pass
        return actual_date, result
    except Exception as e:
        print(f"  [TWSE MI parse ERROR] {e}", flush=True)
        return None, {}


def sync_prices(conn):
    """
    同步策略：
    1. 若 DB 覆蓋率不足 → 用個股月 API 批量補齊歷史
    2. 增量更新 → 用全市場日 API（一次抓全部股票某天資料），速度快很多
    """
    started_at = int(time.time() * 1000)
    total = 0

    row = conn.execute("SELECT MAX(date) FROM stock_prices").fetchone()
    latest_in_db = row[0] if row and row[0] else None

    covered = conn.execute(
        "SELECT COUNT(*) FROM (SELECT symbol, COUNT(*) as c FROM stock_prices GROUP BY symbol HAVING c >= 120)"
    ).fetchone()[0]
    need_bulk = not latest_in_db or covered < len(ALL_STOCKS) * 0.5

    if need_bulk:
        # 歷史補齊：用 MI_INDEX 指定日 API（一天一個 request 抓全市場）
        print(f"歷史資料不足（{covered}/{len(ALL_STOCKS)} 檔），補齊近2年...", flush=True)
        hist_start = (datetime.now() - timedelta(days=730)).date()
        total += _twse_mi_history_bulk(conn, hist_start)
    elif latest_in_db:
        # 增量更新：STOCK_DAY_ALL 只回傳「最新交易日」，不論 date 參數。
        # 策略：先抓最新一天；若與 DB 最新日期有缺口，再用個股月 API 補齊中間。
        today = date.today()
        actual_date, twse_data = fetch_twse_day(today)
        if actual_date and twse_data:
            batch = []
            for code, p in twse_data.items():
                symbol = TSE_SYMBOLS.get(code)
                if symbol:
                    batch.append((symbol, actual_date, p["open"], p["high"], p["low"], p["close"], p["volume"], p["close"]))
            if batch:
                conn.executemany(
                    "INSERT OR REPLACE INTO stock_prices (symbol, date, open, high, low, close, volume, adj_close) VALUES (?,?,?,?,?,?,?,?)",
                    batch
                )
                conn.commit()
                total += len(batch)
                print(f"  {actual_date}: {len(batch)} 檔（最新交易日）", flush=True)

            # 檢查是否有中間缺口：DB 最新日期 → API 最新日期之間還有交易日未補
            api_date_obj = datetime.strptime(actual_date, "%Y-%m-%d").date()
            db_date_obj = datetime.strptime(latest_in_db, "%Y-%m-%d").date()
            if api_date_obj > db_date_obj + timedelta(days=1):
                # 中間有缺口，用 MI_INDEX 指定日 API 補
                gap_start = db_date_obj + timedelta(days=1)
                print(f"偵測到缺口 {gap_start} → {api_date_obj}，用 MI_INDEX 補齊...", flush=True)
                total += _twse_mi_history_bulk(conn, gap_start)
        else:
            print(f"  無法取得 TWSE 最新資料", flush=True)

    log_sync(conn, "prices", "success", total, started_at=started_at)
    print(f"價格同步完成，共新增 {total} 筆", flush=True)
    return total


def _fetch_stock_month(code: str, ym: str) -> list:
    """抓單一上市股票某月的日資料（TWSE 個股日 K API）"""
    rows = []
    try:
        url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={ym}01&stockNo={code}"
        r = SESSION.get(url, timeout=10)
        data = r.json()
        if data.get("stat") != "OK":
            return rows
        # fields: 日期(民國),成交股數,成交金額,開盤價,最高價,最低價,收盤價,漲跌價差,成交筆數
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
    except Exception:
        pass
    return rows


def _twse_mi_history_bulk(conn, start_date: date) -> int:
    """用 MI_INDEX 指定日 API 抓歷史資料。
    每天一個 request（全市場），比個股月 API 快 40+ 倍。
    並行 workers=30，每天抓 ~1000 檔。
    """
    # 取得 DB 中已有的 (symbol, date) pairs，跳過已存在資料
    existing = set()
    rows = conn.execute(
        "SELECT symbol, date FROM stock_prices WHERE date >= ?",
        (start_date.strftime("%Y-%m-%d"),)
    ).fetchall()
    for r in rows:
        existing.add((r[0], r[1]))

    # 建立日期清單（只包含工作日）
    today = date.today()
    days = []
    d = start_date
    while d <= today:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)

    if not days:
        return 0

    print(f"@PROGRESS|prices|0|{len(days)}", flush=True)
    print(f"  用 MI_INDEX API 抓 {len(days)} 個工作日（全市場）...", flush=True)

    def _fetch(d):
        return fetch_twse_mi_index(d)

    total = 0
    batch_results = []
    done = 0
    BATCH_SIZE = 5000
    WORKERS = 15  # TWSE 限流嚴，並發太高會 timeout

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(_fetch, d): d for d in days}
        for fut in as_completed(futures):
            actual_date, day_data = fut.result()
            if actual_date and day_data:
                for code, p in day_data.items():
                    symbol = TSE_SYMBOLS.get(code)
                    if not symbol:
                        continue
                    if (symbol, actual_date) in existing:
                        continue
                    batch_results.append((symbol, actual_date, p["open"], p["high"], p["low"], p["close"], p["volume"], p["close"]))
            done += 1
            if done % 10 == 0 or done == len(days):
                pct = done * 100 // len(days)
                print(f"@PROGRESS|prices|{done}|{len(days)}", flush=True)
                print(f"  進度 {done}/{len(days)} ({pct}%)，暫存 {len(batch_results)} 筆", flush=True)
            if len(batch_results) >= BATCH_SIZE:
                conn.executemany(
                    "INSERT OR REPLACE INTO stock_prices (symbol, date, open, high, low, close, volume, adj_close) VALUES (?,?,?,?,?,?,?,?)",
                    batch_results
                )
                conn.commit()
                total += len(batch_results)
                batch_results = []

    if batch_results:
        conn.executemany(
            "INSERT OR REPLACE INTO stock_prices (symbol, date, open, high, low, close, volume, adj_close) VALUES (?,?,?,?,?,?,?,?)",
            batch_results
        )
        conn.commit()
        total += len(batch_results)

    print(f"  MI_INDEX 歷史抓取完成：{total} 筆", flush=True)
    return total


def _twse_history_bulk(conn, start_date: str) -> int:
    """
    用 TWSE 個股日 K API 並發補齊上市歷史價格。
    已有完整月份（>= stock_prices 該月實際交易日數）跳過。
    """
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    today = date.today()

    # 統計各 (symbol, YYYY-MM) 已有的交易日數
    month_counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT symbol, substr(date,1,7) as ym, COUNT(*) as cnt FROM stock_prices GROUP BY symbol, ym"
    ).fetchall():
        month_counts[f"{row[0]}_{row[1]}"] = row[2]

    # 用「已有最多資料的股票」作為該月交易日數的權威值（通常是同步完整的大型股）
    # 這樣可避免部分同步成功時，用平均或自身筆數當標準而誤判為完整
    market_days_per_month: dict[str, int] = {}
    for row in conn.execute(
        """SELECT ym, MAX(cnt) FROM (
               SELECT symbol, substr(date,1,7) as ym, COUNT(*) as cnt
               FROM stock_prices GROUP BY symbol, ym
           ) GROUP BY ym"""
    ).fetchall():
        market_days_per_month[row[0]] = row[1]

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
        for ym in months:
            ym_key = f"{ym[:4]}-{ym[4:]}"
            key = f"{symbol}_{ym_key}"
            # 最新月永遠重抓（可能有新交易日）
            if ym == last_ym:
                tasks.append((symbol, code, ym))
                continue
            # 該月市場交易日數：取全市場最完整股票的該月筆數（大型股幾乎都滿）
            market_days = market_days_per_month.get(ym_key, 20)
            # 該檔該月筆數必須 >= market_days 才跳過（否則視為不完整要重抓）
            if month_counts.get(key, 0) >= market_days:
                continue
            tasks.append((symbol, code, ym))

    if not tasks:
        print("  無需補齊歷史資料", flush=True)
        return 0

    print(f"@PROGRESS|prices|0|{len(tasks)}", flush=True)
    print(f"  共 {len(tasks)} 個下載任務（{len(ALL_STOCKS)} 檔 × {len(months)} 個月）...", flush=True)
    total = 0
    batch_results = []

    def _dl(task):
        symbol, code, ym = task
        day_rows = _fetch_stock_month(code, ym)
        return symbol, day_rows

    WORKERS = 40
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
                print(f"@PROGRESS|prices|{done}|{len(tasks)}", flush=True)
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


def _fetch_finmind_eps(code: str, start_date: str) -> dict[tuple[int, int], float]:
    """
    用 FinMind 只抓單季 EPS，回傳 {(year, quarter): eps}。
    用來補 yfinance 缺失的 EPS（特別是 Q3 / Q4）。

    注意：FinMind 的 TaiwanStockFinancialStatements 裡 Revenue/NI 數字有時不是單季，
    所以只用 EPS（這個欄位相對可信），其他財報欄位仍由 yfinance 提供。
    """
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockFinancialStatements&data_id={code}&start_date={start_date}"
    r = _retry_get(SESSION, url, timeout=12, retries=1, delay=2, label=f"FinMind EPS {code}")
    if not r:
        return {}
    try:
        data = r.json().get("data", [])
    except Exception:
        return {}

    result = {}
    for row in data:
        if row.get("type") != "EPS":
            continue
        try:
            y, m, _ = row["date"].split("-")
            year, month = int(y), int(m)
            quarter = (month - 1) // 3 + 1
            eps = row.get("value")
            if eps is not None:
                result[(year, quarter)] = eps
        except Exception:
            pass
    return result


def sync_financials(conn):
    """
    同步財務報表（yfinance 主力 + FinMind 補 EPS）。

    策略：
    1. yfinance 抓完整季報（revenue/ni/equity/assets/debt/eps）— 季度有時會缺（例如 Q3）
    2. FinMind 單季 EPS 補回 yfinance 缺失的季度（FinMind 台股 EPS 覆蓋率高、單季連續）
    """
    started_at = int(time.time() * 1000)
    total = 0

    existing = {}
    for row in conn.execute("SELECT symbol, MAX(year*10+quarter) as latest FROM financials GROUP BY symbol").fetchall():
        existing[row[0]] = row[1]
    now_yq = datetime.now().year * 10 + (datetime.now().month - 1) // 3 + 1

    need_dl = [s for s in ALL_STOCKS if existing.get(s, 0) < now_yq]
    print(f"@PROGRESS|financials|0|{len(need_dl)}", flush=True)
    print(f"同步財務報表，{len(need_dl)} 檔需更新...", flush=True)

    # FinMind 補 EPS 用的起始日
    fm_start = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")

    def _fetch_fin(symbol):
        code = _code(symbol)
        # 主力：yfinance
        yf_rows = []
        try:
            ticker = yf.Ticker(symbol)
            income = ticker.quarterly_income_stmt
            balance = ticker.quarterly_balance_sheet
            if income is not None and not income.empty:
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
                        yf_rows.append((year, quarter, revenue, op_profit, net_income, eps, equity, total_assets, total_debt))
                    except Exception:
                        pass
        except Exception:
            pass

        # 補充：FinMind EPS（彌補 yfinance 缺的季度）
        fm_eps_map = _fetch_finmind_eps(code, fm_start)

        # 合併：把 yfinance 的每季資料用 dict 索引，再用 FinMind 補齊 EPS
        by_yq = {(r[0], r[1]): list(r) for r in yf_rows}
        for (y, q), eps in fm_eps_map.items():
            if (y, q) not in by_yq:
                # yfinance 完全沒有這季 → 新增一列（只有 EPS，其他欄位 None）
                by_yq[(y, q)] = [y, q, None, None, None, eps, None, None, None]
            elif by_yq[(y, q)][5] is None:
                # yfinance 有這季但 EPS 缺 → 用 FinMind 補
                by_yq[(y, q)][5] = eps

        rows = [(symbol, *data) for data in by_yq.values()]
        return symbol, rows

    done = 0
    with ThreadPoolExecutor(max_workers=15) as executor:
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
                print(f"@PROGRESS|financials|{done}|{len(need_dl)}", flush=True)
                print(f"  財務進度 {done}/{len(need_dl)}，累計 {total} 筆", flush=True)

    # 回填缺失的 EPS：用同股票其他季度的 EPS/net_income 比例推算
    # 改進：
    # 1. 用最近 4 季有效資料計算中位數比例（避免極端值污染）
    # 2. 檢查 EPS/NI 比例合理範圍（避免除權息/股本變動導致比例異常）
    # 3. 只回填最近 2 年資料（更久的回填無意義）
    from datetime import date
    min_year = date.today().year - 2
    null_eps = conn.execute(
        "SELECT symbol, year, quarter, net_income FROM financials WHERE eps IS NULL AND net_income IS NOT NULL AND year >= ?",
        (min_year,)
    ).fetchall()
    filled = 0
    skipped = 0
    for symbol, year, quarter, net_income in null_eps:
        # 取同股票 EPS/NI 比例中位數（最近 4 季有效資料）
        refs = conn.execute(
            """SELECT eps, net_income FROM financials
               WHERE symbol=? AND eps IS NOT NULL AND net_income IS NOT NULL AND net_income != 0
               ORDER BY ABS((year*10+quarter) - (?*10+?)) LIMIT 4""",
            (symbol, year, quarter)
        ).fetchall()
        if not refs:
            skipped += 1
            continue
        ratios = [r[0] / r[1] for r in refs if r[1] != 0]
        if not ratios:
            skipped += 1
            continue
        # 用中位數（比任何單季更穩定）
        ratios.sort()
        median_ratio = ratios[len(ratios) // 2]
        # 檢查比例合理性：ratio 應該接近 1/股數（通常極小值 ~ 1e-9）
        # 若跨季 ratio 離散度太大（stddev/mean > 30%），跳過避免股本變動污染
        if len(ratios) >= 3:
            mean_r = sum(ratios) / len(ratios)
            if mean_r != 0:
                var = sum((r - mean_r) ** 2 for r in ratios) / len(ratios)
                stddev = var ** 0.5
                cv = stddev / abs(mean_r)
                if cv > 0.3:  # 比例變異過大 → 股本有變動，不可信
                    skipped += 1
                    continue
        estimated_eps = round(net_income * median_ratio, 2)
        conn.execute("UPDATE financials SET eps=? WHERE symbol=? AND year=? AND quarter=?",
                     (estimated_eps, symbol, year, quarter))
        filled += 1
    if filled or skipped:
        conn.commit()
        print(f"  EPS 回填 {filled} 筆（跳過 {skipped} 筆：資料不足或股本有變動）", flush=True)

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
    r = _retry_get(SESSION, url, label="TWSE 三大法人")
    if not r:
        return {}
    try:
        data = r.json()
        if data.get("stat") != "OK":
            return {}
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
        print(f"  [TWSE 三大法人 parse ERROR] {e}", flush=True)
        return {}


def fetch_twse_margin(target_date: date) -> dict[str, dict]:
    """TWSE 個股融資融券餘額"""
    url = f"https://www.twse.com.tw/exchangeReport/MI_MARGN?response=json&date={_twse_date_str(target_date)}&selectType=ALL"
    r = _retry_get(SESSION, url, label="TWSE 融資券")
    if not r:
        return {}
    try:
        data = r.json()
        tables = data.get("tables", [])
        if len(tables) < 2:
            return {}
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
        print(f"  [TWSE 融資券 parse ERROR] {e}", flush=True)
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

    # 用 stock_prices 的實際交易日過濾，避免打假日的 API
    trading_day_rows = conn.execute(
        "SELECT DISTINCT date FROM stock_prices WHERE date >= ? ORDER BY date",
        (price_start.strftime("%Y-%m-%d"),)
    ).fetchall()
    trading_days = set(r[0] for r in trading_day_rows)

    # 需要補的日期：前面的缺口 + 後面的增量
    dates_to_fill = []
    if inst_start and price_start < inst_start:
        # 回填缺口（限定有價格資料的交易日）
        gap_dates = [price_start + timedelta(days=i) for i in range((inst_start - price_start).days)]
        dates_to_fill += [d for d in gap_dates if d.strftime("%Y-%m-%d") in trading_days]

    if latest_in_db:
        start = datetime.strptime(latest_in_db, "%Y-%m-%d").date()
        new_dates = [start + timedelta(days=i) for i in range(1, (today - start).days + 1)]
        # 優先用交易日過濾，超出 stock_prices 範圍的日期用 weekday 過濾
        max_price_date = max(trading_days) if trading_days else ""
        dates_to_fill += [d for d in new_dates if d.strftime("%Y-%m-%d") in trading_days or (d.strftime("%Y-%m-%d") > max_price_date and d.weekday() < 5)]

        # 補回中間空洞：(1) 完全沒資料 (2) 筆數 < stock_prices 80%
        hole_rows = conn.execute(
            """
            SELECT p.date, COUNT(DISTINCT p.symbol) AS price_cnt,
                   (SELECT COUNT(*) FROM institutional i WHERE i.date = p.date) AS inst_cnt
            FROM stock_prices p
            WHERE p.date >= ? AND p.date <= ?
            GROUP BY p.date
            HAVING inst_cnt = 0 OR inst_cnt < price_cnt * 0.8
            ORDER BY p.date
            """,
            (price_start.strftime("%Y-%m-%d"), latest_in_db)
        ).fetchall()
        hole_dates = [datetime.strptime(r[0], "%Y-%m-%d").date() for r in hole_rows]
        incomplete_count = sum(1 for r in hole_rows if r[2] > 0)
        if incomplete_count > 0:
            print(f"偵測到 {incomplete_count} 天籌碼資料不完整（< 80%），一併重抓", flush=True)
        existing_set = set(dates_to_fill)
        for hd in hole_dates:
            if hd not in existing_set:
                dates_to_fill.append(hd)
        if hole_dates:
            dates_to_fill.sort()
    else:
        # 首次同步：用交易日，若無價格資料則用 weekday
        if trading_days:
            dates_to_fill = sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in trading_days)
        else:
            all_dates = [price_start + timedelta(days=i) for i in range((today - price_start).days + 1)]
            dates_to_fill = [d for d in all_dates if d.weekday() < 5]

    if not dates_to_fill:
        print("籌碼資料已是最新", flush=True)
        return 0

    print(f"@PROGRESS|chips|0|{len(dates_to_fill)}", flush=True)
    print(f"同步籌碼資料，{len(dates_to_fill)} 個交易日...", flush=True)
    inst_total = 0
    margin_total = 0

    def _fetch_with_retry(d, kind, fetch_fn, label):
        """帶指數退避重試的抓取"""
        for attempt in range(3):
            try:
                return d, kind, fetch_fn(d)
            except Exception as e:
                if attempt < 2:
                    delay = (attempt + 1) * 2  # 2s, 4s
                    print(f"  [WARN] {d} {label}第{attempt+1}次失敗: {e}，{delay}秒後重試", flush=True)
                    time.sleep(delay)
                else:
                    print(f"  [WARN] {d} {label}抓取失敗（已重試3次）: {e}", flush=True)
                    return d, kind, {}

    def _fetch_inst(d):
        return _fetch_with_retry(d, "inst", fetch_twse_institutional, "法人")

    def _fetch_margin(d):
        return _fetch_with_retry(d, "margin", fetch_twse_margin, "融資券")

    # 並行抓取：法人和融資券拆成獨立任務
    # 降低並發度避免 TWSE 擋住（每批 5 天 × 2 API = 10 並發，批次間 sleep）
    BATCH_SIZE = 5
    for batch_start in range(0, len(dates_to_fill), BATCH_SIZE):
        batch = dates_to_fill[batch_start:batch_start + BATCH_SIZE]

        day_data = {d: {"inst": {}, "margin": {}} for d in batch}
        with ThreadPoolExecutor(max_workers=BATCH_SIZE * 2) as executor:
            futures = []
            for d in batch:
                futures.append(executor.submit(_fetch_inst, d))
                futures.append(executor.submit(_fetch_margin, d))
            for future in as_completed(futures):
                d, kind, data = future.result()
                day_data[d][kind] = data

        for d in sorted(day_data.keys()):
            date_str = d.strftime("%Y-%m-%d")
            tse_inst = day_data[d]["inst"]
            margin = day_data[d]["margin"]

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

            conn.commit()  # 每天 commit，crash 不丟整批

            if day_inst > 0 or day_margin > 0:
                print(f"  {date_str}: 法人 {day_inst} 檔, 融資券 {day_margin} 檔", flush=True)
            else:
                print(f"  {date_str}: 休市或無資料", flush=True)

            inst_total += day_inst
            margin_total += day_margin

        done_days = min(batch_start + BATCH_SIZE, len(dates_to_fill))
        print(f"@PROGRESS|chips|{done_days}|{len(dates_to_fill)}", flush=True)
        time.sleep(0.3)  # 批次間短暫休息

    log_sync(conn, "chips", "success", inst_total + margin_total, started_at=started_at)
    print(f"籌碼同步完成：法人 {inst_total} 筆，融資券 {margin_total} 筆", flush=True)
    return inst_total + margin_total


def _fetch_monthly_revenue_finmind(code: str, start_date: str) -> list[dict]:
    """用 FinMind API 抓單一股票月營收。回傳 [{year, month, revenue}, ...]"""
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockMonthRevenue&data_id={code}&start_date={start_date}"
    r = _retry_get(SESSION, url, timeout=10, retries=1, delay=2, label=f"FinMind {code}")
    if not r:
        return []
    try:
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
                    "revenue": float(rev),
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

    print(f"@PROGRESS|monthly_revenue|0|{len(ALL_STOCKS)}", flush=True)
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
                print(f"@PROGRESS|monthly_revenue|{done}|{len(all_symbols)}", flush=True)
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


def sync_taifex(conn):
    """
    同步外資台指期未平倉（TAIFEX 領先指標）。
    FinMind API 回傳每日各法人（自營/投信/外資）的多空未平倉餘額。
    只保留「外資」的多空 OI 與淨 OI（口數）。
    """
    started_at = int(time.time() * 1000)
    row = conn.execute("SELECT MAX(date) FROM futures_positions").fetchone()
    if row and row[0]:
        start_date = row[0]  # 重抓最新日（可能當日更新）
    else:
        start_date = (date.today() - timedelta(days=730)).strftime("%Y-%m-%d")

    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanFuturesInstitutionalInvestors&data_id=TX&start_date={start_date}"
    r = _retry_get(SESSION, url, timeout=20, retries=2, delay=3, label="TAIFEX")
    if not r:
        print("TAIFEX 同步失敗", flush=True)
        log_sync(conn, "taifex", "error", 0, error="fetch failed", started_at=started_at)
        return 0
    try:
        data = r.json().get("data", [])
    except Exception as e:
        print(f"TAIFEX parse 失敗: {e}", flush=True)
        log_sync(conn, "taifex", "error", 0, error=str(e), started_at=started_at)
        return 0

    # 外資識別關鍵字（中文可能被編碼顯示不出來，用寬鬆比對）
    FOREIGN_KEYWORDS = ["外資", "外陸資", "Foreign"]
    by_date = {}
    for r in data:
        inv = r.get("institutional_investors", "")
        if not any(k in inv for k in FOREIGN_KEYWORDS):
            continue
        d = r["date"]
        long_oi = int(r.get("long_open_interest_balance_volume") or 0)
        short_oi = int(r.get("short_open_interest_balance_volume") or 0)
        by_date[d] = (long_oi, short_oi, long_oi - short_oi)

    total = 0
    for d, (lo, so, net) in by_date.items():
        conn.execute(
            "INSERT OR REPLACE INTO futures_positions (date, foreign_long_oi, foreign_short_oi, foreign_net_oi) VALUES (?,?,?,?)",
            (d, lo, so, net)
        )
        total += 1
    conn.commit()
    log_sync(conn, "taifex", "success", total, started_at=started_at)
    print(f"TAIFEX 同步完成：{total} 筆（外資台指期 OI）", flush=True)
    return total


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    conn = get_conn()

    print("同步股票清單...", flush=True)
    sync_stock_list(conn)

    # 價格 + 籌碼：用新的統一同步引擎
    if mode in ("prices", "prices_chips", "chips", "all"):
        from sync_engine import sync_all
        sync_all(conn, mode="auto")

    if mode in ("financials", "all"):
        sync_financials(conn)

    if mode in ("monthly_revenue", "all"):
        sync_monthly_revenue(conn)

    if mode in ("taifex", "all"):
        sync_taifex(conn)

    conn.close()

    # 同步標籤（不需要 conn，獨立操作）
    try:
        from sync_tags import sync_tags
        sync_tags()
    except Exception as e:
        print(f"[WARN] 標籤同步失敗: {e}", flush=True)

    print("完成！", flush=True)
