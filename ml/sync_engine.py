"""
統一同步引擎 (SyncEngine)

設計原則：
1. 以「日期」為主單位：每個交易日一次 request 抓全市場
2. 驗證每個寫入：API actual_date 必須等於 expected_date
3. 冪等：同一天跑多次結果一致，沒抓到的會繼續補
4. 整合：價格 + 法人 + 融資券一起抓（共用 TWSE session）

三種模式自動偵測：
- bulk: DB 覆蓋率 < 50% → 抓近 2 年全部交易日
- incremental: DB 有資料 → 補 DB 最新日期 ~ 今天的 gap
- verify: 每次同步後，檢查最近 7 天完整性，補漏
"""
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import sys
sys.path.insert(0, str(Path(__file__).parent))
from stock_list import ALL_STOCKS as _RAW
from sync import (
    _retry_get, SESSION, TSE_SYMBOLS,
    fetch_twse_mi_index, fetch_twse_day,
    fetch_twse_institutional, fetch_twse_margin,
    _twse_date_str, log_sync, get_conn,
)

ALL_TSE = [s for s in _RAW if s.endswith(".TW")]
DB_PATH = Path(__file__).parent.parent / "data" / "stock.db"

# 完整性檢查：正常交易日每天應該有 >= MIN_DAILY_COVERAGE 檔股票資料
MIN_DAILY_COVERAGE = int(len(ALL_TSE) * 0.85)  # 85% 以上視為完整


class SyncEngine:
    def __init__(self, conn):
        self.conn = conn
        self.stats = {"prices": 0, "inst": 0, "margin": 0, "days_fetched": 0, "days_skipped": 0}

    # ========================================================
    # 公開 API
    # ========================================================

    def sync(self, mode: str = "auto") -> dict:
        """
        主同步入口。
        mode: 'auto' | 'bulk' | 'incremental' | 'verify'
        """
        started_at = int(time.time() * 1000)

        if mode == "auto":
            mode = self._detect_mode()

        print(f"[SyncEngine] mode={mode}", flush=True)

        if mode == "bulk":
            self._sync_bulk()
        elif mode == "incremental":
            self._sync_incremental()
        elif mode == "verify":
            self._verify_recent()
        else:
            raise ValueError(f"未知 mode: {mode}")

        # 每次同步結束都跑一次完整性檢查（保證最近 14 天完整）
        if mode != "verify":
            print(f"[SyncEngine] 執行最後的完整性檢查...", flush=True)
            self._verify_recent(days=14)

        log_sync(self.conn, "prices", "success", self.stats["prices"], started_at=started_at)
        log_sync(self.conn, "chips", "success", self.stats["inst"] + self.stats["margin"], started_at=started_at)

        print(f"[SyncEngine] 完成：價格 {self.stats['prices']} 筆，法人 {self.stats['inst']} 筆，融資券 {self.stats['margin']} 筆", flush=True)
        print(f"[SyncEngine] 處理 {self.stats['days_fetched']} 個交易日，跳過 {self.stats['days_skipped']} 個（資料已完整）", flush=True)
        return self.stats

    # ========================================================
    # 模式偵測
    # ========================================================

    def _detect_mode(self) -> str:
        """根據 DB 狀態自動選擇模式"""
        latest_row = self.conn.execute("SELECT MAX(date) FROM stock_prices").fetchone()
        latest = latest_row[0] if latest_row and latest_row[0] else None

        covered = self.conn.execute(
            "SELECT COUNT(*) FROM (SELECT symbol FROM stock_prices GROUP BY symbol HAVING COUNT(*) >= 120)"
        ).fetchone()[0]

        if not latest or covered < len(ALL_TSE) * 0.5:
            return "bulk"
        return "incremental"

    # ========================================================
    # 策略 A：Bulk（歷史補齊）
    # ========================================================

    def _sync_bulk(self):
        """首次同步：抓近 2 年所有交易日"""
        start = (datetime.now() - timedelta(days=730)).date()
        today = date.today()
        trading_days = self._gen_trading_days(start, today)
        print(f"[bulk] 抓 {len(trading_days)} 個工作日近 2 年資料...", flush=True)
        self._sync_days(trading_days)

    # ========================================================
    # 策略 B：Incremental（補新資料）
    # ========================================================

    def _sync_incremental(self):
        """日常增量：DB 最新日期 → 今天"""
        latest_row = self.conn.execute("SELECT MAX(date) FROM stock_prices").fetchone()
        latest = latest_row[0] if latest_row and latest_row[0] else None

        if not latest:
            # 不該走到這裡（應該是 bulk），但防呆
            self._sync_bulk()
            return

        today = date.today()
        latest_d = datetime.strptime(latest, "%Y-%m-%d").date()

        if latest_d >= today:
            print(f"[incremental] DB 最新日期 {latest} 已是最新", flush=True)
            return

        # 從 DB 最新日期 +1 開始
        start = latest_d + timedelta(days=1)
        trading_days = self._gen_trading_days(start, today)

        if not trading_days:
            print(f"[incremental] 無新交易日需同步", flush=True)
            return

        print(f"[incremental] 抓 {len(trading_days)} 個新交易日：{trading_days[0]} → {trading_days[-1]}", flush=True)
        self._sync_days(trading_days)

    # ========================================================
    # 策略 C：Verify（完整性檢查補漏）
    # ========================================================

    def _verify_recent(self, days: int = 14):
        """分類型檢查最近 N 天完整性：
        - 價格: 缺漏日期 或 < 85% 覆蓋 → 重抓整天
        - 法人: 該日 0 筆 或 < 80% 價格覆蓋 → 只重抓法人
        - 融資券: 該日 0 筆 或 < 80% → 只重抓融資券
        """
        cutoff = (date.today() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
        price_rows = self.conn.execute(
            "SELECT date, COUNT(DISTINCT symbol) FROM stock_prices WHERE date >= ? GROUP BY date",
            (cutoff,)
        ).fetchall()
        inst_rows = self.conn.execute(
            "SELECT date, COUNT(DISTINCT symbol) FROM institutional WHERE date >= ? GROUP BY date",
            (cutoff,)
        ).fetchall()
        margin_rows = self.conn.execute(
            "SELECT date, COUNT(DISTINCT symbol) FROM margin_trading WHERE date >= ? GROUP BY date",
            (cutoff,)
        ).fetchall()
        price_map = {r[0]: r[1] for r in price_rows}
        inst_map = {r[0]: r[1] for r in inst_rows}
        margin_map = {r[0]: r[1] for r in margin_rows}

        # 近 N 個工作日必須都在 DB 中
        today = date.today()
        expected_days = self._gen_trading_days(today - timedelta(days=days * 2), today)
        recent_expected = expected_days[-days:]

        fix_all = set()      # 整天重抓（價格缺）
        fix_inst = set()     # 只補法人
        fix_margin = set()   # 只補融資券

        for d in recent_expected:
            ds = d.strftime("%Y-%m-%d")
            price_cnt = price_map.get(ds, 0)
            if price_cnt < MIN_DAILY_COVERAGE:
                fix_all.add(d)
                continue
            inst_cnt = inst_map.get(ds, 0)
            margin_cnt = margin_map.get(ds, 0)
            # inst=0 或 < 80% price → 補法人
            if inst_cnt == 0 or inst_cnt < price_cnt * 0.8:
                fix_inst.add(d)
            # margin=0 或 < 80% price → 補融資券
            if margin_cnt == 0 or margin_cnt < price_cnt * 0.8:
                fix_margin.add(d)

        # 已檢查過的歷史日期（DB 內有價格，但 inst/margin 有漏）
        for ds, price_cnt in price_map.items():
            d = datetime.strptime(ds, "%Y-%m-%d").date()
            if d in fix_all:
                continue
            inst_cnt = inst_map.get(ds, 0)
            margin_cnt = margin_map.get(ds, 0)
            if inst_cnt == 0 or inst_cnt < price_cnt * 0.8:
                fix_inst.add(d)
            if margin_cnt == 0 or margin_cnt < price_cnt * 0.8:
                fix_margin.add(d)

        if not (fix_all or fix_inst or fix_margin):
            print(f"[verify] 最近 {days} 天完整", flush=True)
            return

        if fix_all:
            print(f"[verify] 重抓整天 {len(fix_all)} 天：{sorted(d.strftime('%Y-%m-%d') for d in fix_all)}", flush=True)
            self._sync_days(sorted(fix_all))

        chip_only = sorted(fix_inst | fix_margin)
        if chip_only:
            print(f"[verify] 補法人/融資券 {len(chip_only)} 天（inst={len(fix_inst)}, margin={len(fix_margin)}）", flush=True)
            self._sync_chips_only(chip_only, fix_inst, fix_margin)

    def _sync_chips_only(self, days: list, need_inst: set, need_margin: set):
        """只補法人/融資券，不重抓價格。針對 inst=0 的空洞特別有效。"""
        total = len(days)
        if total == 0:
            return

        def _fetch(d: date):
            result = {"date": d, "inst": None, "margin": None}
            if d in need_inst:
                try:
                    for attempt in range(3):
                        data = fetch_twse_institutional(d)
                        if data:
                            result["inst"] = data
                            break
                        time.sleep((attempt + 1) * 2)
                except Exception as e:
                    print(f"  [WARN] {d} 法人重抓失敗: {e}", flush=True)
            if d in need_margin:
                try:
                    for attempt in range(3):
                        data = fetch_twse_margin(d)
                        if data:
                            result["margin"] = data
                            break
                        time.sleep((attempt + 1) * 2)
                except Exception as e:
                    print(f"  [WARN] {d} 融資券重抓失敗: {e}", flush=True)
            return result

        WORKERS = 15
        BATCH = WORKERS
        for i in range(0, total, BATCH):
            batch = days[i:i + BATCH]
            with ThreadPoolExecutor(max_workers=len(batch)) as ex:
                futures = {ex.submit(_fetch, d): d for d in batch}
                for fut in as_completed(futures):
                    r = fut.result()
                    d = r["date"]
                    date_str = d.strftime("%Y-%m-%d")
                    if r["inst"]:
                        rows = []
                        for code, v in r["inst"].items():
                            sym = TSE_SYMBOLS.get(code)
                            if sym:
                                rows.append((sym, date_str, v["foreign_net"], v["trust_net"], v["dealer_net"], v["total_net"]))
                        if rows:
                            self.conn.executemany(
                                "INSERT OR REPLACE INTO institutional (symbol, date, foreign_net, trust_net, dealer_net, total_net) VALUES (?,?,?,?,?,?)",
                                rows
                            )
                            self.stats["inst"] += len(rows)
                            print(f"  {date_str}: 補法人 {len(rows)} 檔", flush=True)
                    if r["margin"]:
                        rows = []
                        for code, v in r["margin"].items():
                            sym = TSE_SYMBOLS.get(code)
                            if sym:
                                rows.append((sym, date_str, v["margin_buy"], v["margin_sell"], v["margin_balance"], v["short_buy"], v["short_sell"], v["short_balance"]))
                        if rows:
                            self.conn.executemany(
                                "INSERT OR REPLACE INTO margin_trading (symbol, date, margin_buy, margin_sell, margin_balance, short_buy, short_sell, short_balance) VALUES (?,?,?,?,?,?,?,?)",
                                rows
                            )
                            self.stats["margin"] += len(rows)
                            print(f"  {date_str}: 補融資券 {len(rows)} 檔", flush=True)
                    self.conn.commit()
            time.sleep(0.15)

    # ========================================================
    # 核心：同步指定的日期清單
    # ========================================================

    def _sync_days(self, trading_days: list):
        """
        對每個交易日併發抓 3 個 API（價格 + 法人 + 融資券），寫入 DB。
        用 MI_INDEX 抓價格，T86 抓法人，MI_MARGN 抓融資券。
        """
        total = len(trading_days)
        if total == 0:
            return

        # 已有 (symbol, date) pairs，跳過已存在的價格（其他資料用 INSERT OR REPLACE）
        existing_prices = self._load_existing_prices(trading_days[0])

        print(f"@PROGRESS|sync|0|{total}", flush=True)

        # 以日期為單位併發（一天 3 個 API 任務）
        WORKERS = 15  # TWSE 實測上限約 20，留緩衝

        done = 0

        def _fetch_day(d: date):
            """抓單一交易日的所有資料（價格 + 法人 + 融資券）"""
            results = {"date": d, "prices": None, "inst": {}, "margin": {}}

            # 價格（MI_INDEX）
            try:
                actual_date, price_data = fetch_twse_mi_index(d)
                expected = d.strftime("%Y-%m-%d")
                if actual_date == expected and price_data:
                    results["prices"] = (expected, price_data)
                elif actual_date and actual_date != expected:
                    # API 回傳的日期不等於預期 → 假日或 API 異常，跳過
                    pass
            except Exception as e:
                print(f"  [WARN] {d} 價格錯誤: {e}", flush=True)

            # 法人 + 融資券（僅交易日抓，避免假日無效請求）
            if results["prices"]:
                try:
                    results["inst"] = fetch_twse_institutional(d)
                except Exception as e:
                    print(f"  [WARN] {d} 法人錯誤: {e}", flush=True)
                try:
                    results["margin"] = fetch_twse_margin(d)
                except Exception as e:
                    print(f"  [WARN] {d} 融資券錯誤: {e}", flush=True)

            return results

        # 分批執行（每批 WORKERS 天，批次間 sleep 0.5 秒緩衝）
        BATCH = WORKERS
        for i in range(0, total, BATCH):
            batch = trading_days[i:i + BATCH]
            with ThreadPoolExecutor(max_workers=len(batch)) as ex:
                futures = {ex.submit(_fetch_day, d): d for d in batch}
                day_results = []
                for fut in as_completed(futures):
                    day_results.append(fut.result())

            # 寫入這批的結果
            for r in day_results:
                self._write_day(r, existing_prices)
                done += 1
                if done % 10 == 0 or done == total:
                    pct = done * 100 // total
                    print(f"@PROGRESS|sync|{done}|{total}", flush=True)
                    print(f"  進度 {done}/{total} ({pct}%)", flush=True)

            time.sleep(0.15)  # 批次間緩衝，避免 TWSE 限速

    def _write_day(self, result: dict, existing_prices: set):
        """寫入單一日的價格 + 法人 + 融資券"""
        d = result["date"]
        date_str = d.strftime("%Y-%m-%d")

        # 寫入價格
        if result["prices"]:
            _, price_data = result["prices"]
            rows = []
            for code, p in price_data.items():
                symbol = TSE_SYMBOLS.get(code)
                if not symbol:
                    continue
                # 跳過已存在的（避免 bulk 模式重複寫入）
                if (symbol, date_str) in existing_prices:
                    continue
                # 驗證資料合理性
                if not (p.get("close") and p["close"] > 0):
                    continue
                rows.append((symbol, date_str, p["open"], p["high"], p["low"],
                            p["close"], p["volume"], p["close"]))
                existing_prices.add((symbol, date_str))
            if rows:
                self.conn.executemany(
                    "INSERT OR REPLACE INTO stock_prices (symbol, date, open, high, low, close, volume, adj_close) VALUES (?,?,?,?,?,?,?,?)",
                    rows
                )
                self.stats["prices"] += len(rows)
            self.stats["days_fetched"] += 1
        else:
            self.stats["days_skipped"] += 1

        # 寫入法人
        if result["inst"]:
            rows = []
            for code, v in result["inst"].items():
                symbol = TSE_SYMBOLS.get(code)
                if symbol:
                    rows.append((symbol, date_str, v["foreign_net"], v["trust_net"],
                                v["dealer_net"], v["total_net"]))
            if rows:
                self.conn.executemany(
                    "INSERT OR REPLACE INTO institutional (symbol, date, foreign_net, trust_net, dealer_net, total_net) VALUES (?,?,?,?,?,?)",
                    rows
                )
                self.stats["inst"] += len(rows)

        # 寫入融資券
        if result["margin"]:
            rows = []
            for code, v in result["margin"].items():
                symbol = TSE_SYMBOLS.get(code)
                if symbol:
                    rows.append((symbol, date_str, v["margin_buy"], v["margin_sell"],
                                v["margin_balance"], v["short_buy"], v["short_sell"], v["short_balance"]))
            if rows:
                self.conn.executemany(
                    "INSERT OR REPLACE INTO margin_trading (symbol, date, margin_buy, margin_sell, margin_balance, short_buy, short_sell, short_balance) VALUES (?,?,?,?,?,?,?,?)",
                    rows
                )
                self.stats["margin"] += len(rows)

        self.conn.commit()

    # ========================================================
    # Helpers
    # ========================================================

    def _gen_trading_days(self, start: date, end: date) -> list:
        """產生 [start, end] 之間的所有工作日（週一到週五）。
        API 會在假日回傳空資料，我們靠這個過濾。
        """
        days = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                days.append(d)
            d += timedelta(days=1)
        return days

    def _load_existing_prices(self, min_date: date) -> set:
        """載入 DB 中 >= min_date 的 (symbol, date) pairs"""
        cutoff = min_date.strftime("%Y-%m-%d")
        rows = self.conn.execute(
            "SELECT symbol, date FROM stock_prices WHERE date >= ?",
            (cutoff,)
        ).fetchall()
        return set((r[0], r[1]) for r in rows)


# ========================================================
# CLI 入口
# ========================================================

def sync_all(conn, mode="auto"):
    """對外：取代舊的 sync_prices + sync_chips"""
    engine = SyncEngine(conn)
    return engine.sync(mode=mode)


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    conn = get_conn()
    sync_all(conn, mode=mode)
    conn.close()
