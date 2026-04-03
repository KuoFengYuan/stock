"""
規則引擎：計算技術指標 + 基本面指標，產生推薦清單
用法：python ml/rule_engine.py
規則分數來源：優先使用 rule_scores.json（由 backtest.py 產生），若無則使用硬編碼 fallback。
"""
import sys
import sqlite3
import time
import json
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np
import pandas_ta as ta

DB_PATH = Path(__file__).parent.parent / "data" / "stock.db"
RULE_SCORES_PATH = Path(__file__).parent / "rule_scores.json"


def _load_rule_scores() -> tuple[dict, set]:
    """
    載入回測產生的規則分數。
    回傳 (scores_dict, suppressed_set)
    - scores_dict: {rule_name: score}，未命中則用 fallback
    - suppressed_set: 勝率 < 50% 的規則，不計入評分
    """
    if not RULE_SCORES_PATH.exists():
        return {}, set()
    try:
        with open(RULE_SCORES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        scores = {}
        suppressed = set()
        # 取市場個股基準勝率；若無則預設 0.45
        mkt_baseline = data.get("market_abs_win_rate", 0.45)
        if mkt_baseline == 0.45:
            # 相容舊格式：從任一 ok 規則讀取
            for v in data.get("rules", {}).values():
                if v.get("status") == "ok" and v.get("market_abs_win_rate") is not None:
                    mkt_baseline = v["market_abs_win_rate"]
                    break
        for rule, v in data.get("rules", {}).items():
            scores[rule] = v["score"]
            # 抑制：絕對勝率低於市場基準（說明觸發後不如「隨便買任一股票」）
            if v["status"] == "ok" and v.get("win_rate") is not None and v["win_rate"] < mkt_baseline:
                suppressed.add(rule)
        return scores, suppressed
    except Exception:
        return {}, set()


_RULE_SCORES, _SUPPRESSED_RULES = _load_rule_scores()


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def calc_indicators(df: pd.DataFrame) -> dict:
    """計算常用技術指標，輸入 OHLCV DataFrame，index 為日期"""
    if len(df) < 30:
        return {}

    close = df["close"]
    result = {}

    # RSI
    rsi = ta.rsi(close, length=14)
    if rsi is not None and not rsi.empty:
        result["rsi14"] = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else None

    # MACD
    macd = ta.macd(close, fast=12, slow=26, signal=9)
    if macd is not None and not macd.empty:
        macd_val = macd.iloc[-1]
        result["macd"] = float(macd_val.get("MACD_12_26_9", 0) or 0)
        result["macd_signal"] = float(macd_val.get("MACDs_12_26_9", 0) or 0)
        result["macd_hist"] = float(macd_val.get("MACDh_12_26_9", 0) or 0)
        # 前一根
        if len(macd) >= 2:
            prev = macd.iloc[-2]
            result["macd_hist_prev"] = float(prev.get("MACDh_12_26_9", 0) or 0)

    # SMA
    sma20 = ta.sma(close, length=20)
    sma60 = ta.sma(close, length=60)
    if sma20 is not None:
        result["sma20"] = float(sma20.iloc[-1]) if not pd.isna(sma20.iloc[-1]) else None
    if sma60 is not None and len(sma60.dropna()) > 0:
        result["sma60"] = float(sma60.iloc[-1]) if not pd.isna(sma60.iloc[-1]) else None

    # Bollinger Bands
    bb = ta.bbands(close, length=20, std=2)
    if bb is not None and not bb.empty:
        bbu_col = next((c for c in bb.columns if c.startswith("BBU_")), None)
        bbl_col = next((c for c in bb.columns if c.startswith("BBL_")), None)
        if bbu_col:
            result["bb_upper"] = float(bb[bbu_col].iloc[-1] or 0)
        if bbl_col:
            result["bb_lower"] = float(bb[bbl_col].iloc[-1] or 0)

    # 成交量均量比
    vol = df["volume"]
    vol20 = vol.rolling(20).mean()
    if not pd.isna(vol20.iloc[-1]) and vol20.iloc[-1] > 0:
        result["vol_ratio"] = float(vol.iloc[-1] / vol20.iloc[-1])

    # 近期漲幅
    if len(close) >= 21:
        result["return20d"] = float((close.iloc[-1] - close.iloc[-21]) / close.iloc[-21] * 100)
    if len(close) >= 61:
        result["return60d"] = float((close.iloc[-1] - close.iloc[-61]) / close.iloc[-61] * 100)

    result["close"] = float(close.iloc[-1])
    result["volume"] = int(df["volume"].iloc[-1])

    return result


def calc_fundamentals(symbol: str, conn) -> dict:
    """計算基本面指標"""
    rows = conn.execute(
        """SELECT year, quarter, revenue, net_income, eps, equity, total_assets, total_debt
           FROM financials WHERE symbol=? ORDER BY year DESC, quarter DESC LIMIT 8""",
        (symbol,)
    ).fetchall()

    if not rows:
        return {}

    result = {}
    latest = rows[0]

    # 推算每股股數（從有 eps 的季度中取，用於補算 eps=None 的季度）
    shares = None
    for r in rows[:4]:
        if r["eps"] and r["eps"] != 0 and r["net_income"]:
            shares = r["net_income"] / r["eps"]
            if shares > 0:
                break

    # 最新 EPS（TTM = 最近4季加總；eps=None 但有 net_income 時用股數反推）
    eps_parts = []
    for r in rows[:4]:
        if r["eps"] is not None:
            eps_parts.append(r["eps"])
        elif r["net_income"] is not None and shares and shares > 0:
            eps_parts.append(r["net_income"] / shares)
    if eps_parts:
        result["eps_ttm"] = sum(eps_parts)

    # TTM 淨利（最近4季加總）
    ni_list = [r["net_income"] for r in rows[:4] if r["net_income"] is not None]
    if len(ni_list) >= 2:  # 至少2季才算
        result["ni_ttm"] = sum(ni_list)

    # 股東權益
    equity = latest["equity"]
    if equity and equity > 0:
        result["equity"] = equity

    # ROE（TTM net income / equity）
    if ni_list and equity and equity > 0:
        result["roe"] = sum(ni_list) / equity * 100

    # 負債比
    if latest["total_assets"] and latest["total_assets"] > 0 and latest["total_debt"] is not None:
        result["debt_ratio"] = latest["total_debt"] / latest["total_assets"] * 100

    # 營收 YoY（最新季 vs 去年同季）
    if rows[0]["revenue"]:
        result["revenue_abs"] = rows[0]["revenue"]
    if len(rows) >= 5 and rows[0]["revenue"] and rows[4]["revenue"] and rows[4]["revenue"] > 0:
        result["revenue_yoy"] = (rows[0]["revenue"] - rows[4]["revenue"]) / rows[4]["revenue"] * 100

    # 淨利 YoY（基期需為正且有一定規模，避免從虧損微轉盈造成失真大數）
    if len(rows) >= 5 and rows[0]["net_income"] and rows[4]["net_income"]:
        base_ni = rows[0]["net_income"]  # 當期
        prev_ni = rows[4]["net_income"]  # 基期（去年同季）
        # 基期需 > 0 且 > 當期的 5%（避免基期極小造成虛高 YoY）
        if prev_ni > 0 and prev_ni > abs(base_ni) * 0.05:
            yoy = (base_ni - prev_ni) / prev_ni * 100
            if yoy <= 500:  # 超過 500% 視為基期過低，不採用
                result["ni_yoy"] = yoy

    return result


def calc_pe_pb(symbol: str, conn, close: float) -> dict:
    """計算 PE / PB（需要股價）"""
    rows = conn.execute(
        "SELECT net_income, eps, equity FROM financials WHERE symbol=? ORDER BY year DESC, quarter DESC LIMIT 4",
        (symbol,)
    ).fetchall()
    if not rows or close <= 0:
        return {}
    result = {}

    # 推算每股股數（從有 eps 的季度中取）
    shares = None
    for r in rows:
        if r["eps"] and r["eps"] != 0 and r["net_income"]:
            shares = r["net_income"] / r["eps"]
            if shares > 0:
                break

    # EPS TTM：優先用財報 eps，若某季 eps=None 但 net_income 有值則用股數反推
    eps_ttm_parts = []
    for r in rows:
        if r["eps"] is not None:
            eps_ttm_parts.append(r["eps"])
        elif r["net_income"] is not None and shares and shares > 0:
            eps_ttm_parts.append(r["net_income"] / shares)

    eps_ttm = sum(eps_ttm_parts) if eps_ttm_parts else None
    if eps_ttm and eps_ttm > 0:
        result["pe_ratio"] = close / eps_ttm

    # PB：用最新季 equity / shares
    r0 = rows[0]
    if shares and r0["equity"] and r0["equity"] > 0:
        bvps = r0["equity"] / shares
        result["pb_ratio"] = close / bvps
    return result


def calc_monthly_revenue(symbol: str, conn) -> dict:
    """
    計算月營收連續成長指標。
    回傳：
      - rev_consecutive_yoy: 最近連續幾個月 YoY > 0（月增年增）
      - rev_consecutive_mom: 最近連續幾個月 MoM > 0（月增月增）
      - rev_accel: 最新月 YoY 是否高於前月 YoY（加速成長）
    """
    rows = conn.execute(
        """SELECT year, month, revenue, yoy, mom
           FROM monthly_revenue WHERE symbol=?
           ORDER BY year DESC, month DESC LIMIT 12""",
        (symbol,)
    ).fetchall()

    if len(rows) < 2:
        return {}

    result = {}

    # 連續 YoY > 0 的月數
    consecutive_yoy = 0
    for r in rows:
        if r["yoy"] is not None and r["yoy"] > 0:
            consecutive_yoy += 1
        else:
            break
    if consecutive_yoy > 0:
        result["rev_consecutive_yoy"] = consecutive_yoy

    # 連續 MoM > 0 的月數
    consecutive_mom = 0
    for r in rows:
        if r["mom"] is not None and r["mom"] > 0:
            consecutive_mom += 1
        else:
            break
    if consecutive_mom > 0:
        result["rev_consecutive_mom"] = consecutive_mom

    # 加速成長：最新月 YoY > 前月 YoY（動能加速）
    if len(rows) >= 2 and rows[0]["yoy"] is not None and rows[1]["yoy"] is not None:
        if rows[0]["yoy"] > rows[1]["yoy"] > 0:
            result["rev_accel"] = True

    return result


def _calc_high_1y(df: pd.DataFrame) -> float | None:
    """
    計算近一年（約 250 個交易日）有效高點。
    若期間有除權/股票分割（相鄰收盤跌超過 30%），
    只取最後一次除權事件之後的資料，避免前後價格基準不同導致跌幅失真。
    """
    if "high" not in df.columns or "close" not in df.columns:
        return None
    recent = df.tail(250)
    close = recent["close"]
    # 找最後一次大幅跳水（相鄰日跌超過 30%）
    ratios = close / close.shift(1)
    split_mask = ratios < 0.70
    if split_mask.any():
        last_split_loc = split_mask[split_mask].index[-1]
        recent = recent.loc[last_split_loc:]
    return float(recent["high"].max()) if not recent.empty else None


def _s(rule: str, fallback: float) -> float:
    """取得規則分數：優先用回測勝率，否則用 fallback"""
    return _RULE_SCORES.get(rule, fallback)


def _add(rule: str, fallback: float, score_parts: list):
    """若規則未被抑制則加入評分"""
    if rule not in _SUPPRESSED_RULES:
        score_parts.append(_s(rule, fallback))


def apply_rules(tech: dict, fund: dict, close: float, monthly: dict | None = None,
                market_win_rate: float = 0.5) -> tuple[list[str], str, float]:
    """
    中長期選股邏輯（3-12 個月持有）。

    評分架構：分層加權，非簡單平均
    ┌─ 基礎分（base_score）：由基本面品質決定，0.45–0.70
    ├─ 估值乘數（val_mult）：估值合理加成、過高懲罰
    ├─ 月營收乘數（rev_mult）：月營收趨勢加成（最多 1 個乘數）
    ├─ 籌碼乘數（chip_mult）：法人方向加成／懲罰
    └─ 技術調整（tech_adj）：進場時機微調，範圍 ±0.05

    最終 score = base_score × val_mult × rev_mult × chip_mult + tech_adj
    動態門檻：buy/watch 門檻隨市場近期勝率調整
    """
    reasons = []
    has_fundamental = False

    # ══ 前置過濾：直接 neutral，不進評分 ══

    high_1y = tech.get("high_1y")
    if high_1y is not None and high_1y > 0 and close > 0:
        drawdown = (close - high_1y) / high_1y
        if drawdown < -0.6:
            return [f"⚠ 近一年跌幅 {drawdown*100:.0f}%"], "neutral", 0.3

    roe_check = fund.get("roe")
    ni_ttm_check = fund.get("ni_ttm")
    if roe_check is not None and roe_check < 0:
        return [], "neutral", 0.3
    if ni_ttm_check is not None and ni_ttm_check < 0:
        return [], "neutral", 0.3

    _pe_early = fund.get("pe_ratio")
    if _pe_early is not None and _pe_early > 60:
        return [f"⚠ PE {_pe_early:.0f} 過高，估值極度偏貴"], "neutral", 0.3

    # ══ 第一層：基本面品質 → 決定 base_score ══
    # 設計：各條件貢獻加分值，總和上限 0.70；無任何訊號 → neutral
    # base_score 範圍：0.45（空手）~ 0.70（全命中）

    base_score = 0.45  # 沒有任何基本面訊號的預設底分

    roe = fund.get("roe")
    if roe is not None:
        if roe >= 20:
            reasons.append(f"ROE {roe:.1f}%")
            base_score += _s("roe_high", 0.20)
            has_fundamental = True
        elif roe >= 12:
            reasons.append(f"ROE {roe:.1f}%")
            base_score += _s("roe_ok", 0.10)
            has_fundamental = True

    revenue_yoy = fund.get("revenue_yoy")
    revenue_abs = fund.get("revenue_abs")
    if revenue_yoy is not None and revenue_yoy > 5 and revenue_abs is not None and revenue_abs >= 1e8:
        reasons.append(f"營收 YoY +{revenue_yoy:.0f}%")
        base_score += _s("revenue_yoy", 0.08)
        has_fundamental = True

    ni_yoy = fund.get("ni_yoy")
    if ni_yoy is not None and ni_yoy > 10:
        reasons.append(f"獲利 YoY +{ni_yoy:.0f}%")
        base_score += _s("ni_yoy", 0.12)
        has_fundamental = True

    debt_ratio = fund.get("debt_ratio")
    if debt_ratio is not None and debt_ratio < 50:
        base_score += _s("debt_low", 0.04)

    base_score = min(base_score, 0.70)  # 上限

    if not has_fundamental:
        return [], "neutral", 0.3

    # ══ 第二層：估值乘數（val_mult）══
    # 合理估值加成；過高估值懲罰；乘數範圍 0.70 ~ 1.15

    pe = fund.get("pe_ratio")
    pb = fund.get("pb_ratio")
    val_mult = 1.0

    if pe is not None:
        if pe > 40:                      # PE 40-60（>60 已被前置過濾）
            val_mult *= 0.75
        elif pe > 30:
            val_mult *= 0.85
        elif pe < 15:
            reasons.append(f"低本益比 PE {pe:.0f}")
            val_mult *= 1.10
        elif pe <= 25:
            reasons.append(f"PE {pe:.0f}")
            val_mult *= 1.03

    if pb is not None:
        if pb < 2:
            reasons.append(f"PB {pb:.1f}")
            val_mult *= 1.05
        elif pb > 4:
            val_mult *= 0.90

    val_mult = max(0.70, min(val_mult, 1.15))

    # ══ 第三層：月營收乘數（rev_mult）══
    # 月營收最多貢獻 1 個乘數，避免多條月營收條件堆疊票數
    # 選最強的一條，其餘只顯示 reason 不再加乘

    rev_mult = 1.0
    if monthly:
        consecutive_yoy = monthly.get("rev_consecutive_yoy", 0)
        consecutive_mom = monthly.get("rev_consecutive_mom", 0)
        rev_accel = monthly.get("rev_accel", False)

        if consecutive_yoy >= 6:
            reasons.append(f"月營收年增連 {consecutive_yoy} 個月")
            rev_mult = _s("rev_yoy_6m", 1.12)
            has_fundamental = True
            if consecutive_mom >= 3:
                reasons.append(f"月營收月增連 {consecutive_mom} 個月")  # 僅顯示，不再疊加乘數
            if rev_accel:
                reasons.append("月營收成長加速")
        elif consecutive_yoy >= 3:
            reasons.append(f"月營收年增連 {consecutive_yoy} 個月")
            rev_mult = _s("rev_yoy_3m", 1.07)
            has_fundamental = True
            if rev_accel:
                reasons.append("月營收成長加速")
        elif consecutive_mom >= 3:
            reasons.append(f"月營收月增連 {consecutive_mom} 個月")
            rev_mult = _s("rev_mom_3m", 1.04)
        elif rev_accel:
            reasons.append("月營收成長加速")
            rev_mult = _s("rev_accel", 1.03)

    rev_mult = max(1.0, min(rev_mult, 1.15))

    # ══ 第四層：籌碼乘數（chip_mult）══
    # 60日方向決定加成/中性；近10日同步賣超施加懲罰
    # 乘數範圍 0.80 ~ 1.10

    foreign_60d = tech.get("foreign_net_60d") or tech.get("foreign_net_20d")
    trust_60d   = tech.get("trust_net_60d")   or tech.get("trust_net_20d")
    foreign_10d = tech.get("foreign_net_10d")
    trust_10d   = tech.get("trust_net_10d")

    chip_mult = 1.0
    if foreign_60d is not None and foreign_60d > 0:
        reasons.append(f"外資60日淨買 {foreign_60d/1e3:.0f}K")
        chip_mult *= 1.05
    if trust_60d is not None and trust_60d > 0:
        reasons.append(f"投信60日淨買 {trust_60d/1e3:.0f}K")
        chip_mult *= 1.05

    foreign_selling = foreign_10d is not None and foreign_10d < 0
    foreign_buying  = foreign_10d is not None and foreign_10d > 0
    trust_selling   = trust_10d   is not None and trust_10d   < 0
    trust_buying    = trust_10d   is not None and trust_10d   > 0
    chip_warning    = foreign_selling and trust_selling

    if foreign_buying and trust_selling:
        f, t = abs(foreign_10d), abs(trust_10d)
        if t > f * 3:
            reasons.append("⚠ 投信大賣／外資小買（近10日）")
            chip_mult *= 0.88
        else:
            reasons.append("外資買超／投信賣超（近10日）")
    elif foreign_selling and trust_buying:
        f, t = abs(foreign_10d), abs(trust_10d)
        if f > t * 3:
            reasons.append("⚠ 外資大賣／投信小買（近10日）")
            chip_mult *= 0.88
        else:
            reasons.append("投信買超／外資賣超（近10日）")

    if chip_warning:
        reasons.append("⚠ 法人近10日同步賣超")
        chip_mult *= 0.82

    chip_mult = max(0.80, min(chip_mult, 1.10))

    # ══ 第五層：技術調整（tech_adj）══
    # 加減微調，上限 ±0.05，不影響整體量級

    tech_adj = 0.0
    rsi = tech.get("rsi14")
    sma60 = tech.get("sma60")
    return20d = tech.get("return20d")

    if rsi is not None:
        if rsi < 35:
            reasons.append("RSI 低檔")
            tech_adj += 0.03
        elif rsi > 75:
            tech_adj -= 0.03

    if sma60 and close:
        if close >= sma60 * 0.95:
            tech_adj += 0.02
        else:
            tech_adj -= 0.03

    if return20d is not None and -10 < return20d < 0:
        reasons.append("近期回調")
        tech_adj += 0.02

    tech_adj = max(-0.05, min(tech_adj, 0.05))

    # ══ 計算最終分數 ══

    score = base_score * val_mult * rev_mult * chip_mult + tech_adj
    score = min(max(score, 0.0), 1.0)

    # ══ 動態門檻：隨市場近期勝率調整 ══
    # market_win_rate 是近期所有個股 60 日正報酬率
    # 牛市（勝率高）→ 門檻略提高；熊市（勝率低）→ 門檻略降低
    # 基準：market_win_rate = 0.50 時，buy=0.56, watch=0.50
    buy_thresh   = 0.56 + (market_win_rate - 0.50) * 0.20
    watch_thresh = 0.50 + (market_win_rate - 0.50) * 0.20
    buy_thresh   = max(0.52, min(buy_thresh,   0.62))
    watch_thresh = max(0.46, min(watch_thresh, 0.56))

    if not reasons:
        signal = "neutral"
    elif score >= buy_thresh:
        signal = "buy"
    elif score >= watch_thresh:
        signal = "watch"
    else:
        signal = "neutral"

    return reasons, signal, score


def _calc_market_win_rate(conn) -> float:
    """
    計算近 60 個交易日市場整體個股正報酬率（作為動態門檻基準）。
    取所有股票最近 61 筆收盤，計算 60 日報酬，統計正報酬比例。
    """
    try:
        rows = conn.execute(
            """SELECT symbol, close, date FROM stock_prices
               WHERE date >= (SELECT date FROM stock_prices ORDER BY date DESC LIMIT 1 OFFSET 65)
               ORDER BY symbol, date ASC"""
        ).fetchall()
        if not rows:
            return 0.50
        from collections import defaultdict
        prices: dict = defaultdict(list)
        for r in rows:
            prices[r["symbol"]].append(r["close"])
        wins, total = 0, 0
        for sym, cls in prices.items():
            if len(cls) >= 61:
                ret = (cls[-1] - cls[-61]) / cls[-61] if cls[-61] else 0
                if abs(ret) <= 0.5:  # 排除極端值
                    total += 1
                    if ret > 0:
                        wins += 1
        rate = wins / total if total > 0 else 0.50
        return float(rate)
    except Exception:
        return 0.50


def run_rule_engine():
    conn = get_conn()
    started_at = int(time.time() * 1000)

    # 取得最新交易日
    row = conn.execute(
        "SELECT date FROM stock_prices ORDER BY date DESC LIMIT 1"
    ).fetchone()
    if not row:
        print("無價格資料，請先執行 sync.py prices", flush=True)
        return
    latest_date = row["date"]
    print(f"分析日期：{latest_date}", flush=True)

    # 計算市場近期勝率（動態門檻用）
    market_win_rate = _calc_market_win_rate(conn)
    print(f"市場近期勝率：{market_win_rate:.1%}（動態門檻基準）", flush=True)

    symbols = [r["symbol"] for r in conn.execute("SELECT symbol FROM stocks").fetchall()]
    count = 0

    for symbol in symbols:
        try:
            # 載入近120日價格
            rows = conn.execute(
                """SELECT date, open, high, low, close, volume
                   FROM stock_prices WHERE symbol=? ORDER BY date ASC""",
                (symbol,)
            ).fetchall()

            if not rows:
                continue

            df = pd.DataFrame([dict(r) for r in rows])
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")

            # 只用最新資料的收盤價
            latest_price_row = conn.execute(
                "SELECT close, volume FROM stock_prices WHERE symbol=? ORDER BY date DESC LIMIT 1",
                (symbol,)
            ).fetchone()
            if not latest_price_row:
                continue

            close = latest_price_row["close"]
            volume = latest_price_row["volume"]

            tech = calc_indicators(df)
            if not tech:
                continue

            # 近一年最高點（用於趨勢崩潰過濾）
            # 偵測除權/股票分割：若相鄰兩日收盤跌超過 30%，視為除權事件
            # 只用最後一次除權事件之後的資料計算高點，避免舊價格污染
            tech["high_1y"] = _calc_high_1y(df)

            # 籌碼：60日累計（長期方向）+ 近10日（短期趨勢）
            inst_rows = conn.execute(
                "SELECT foreign_net, trust_net FROM institutional WHERE symbol=? ORDER BY date DESC LIMIT 60",
                (symbol,)
            ).fetchall()
            if inst_rows:
                tech["foreign_net_60d"] = sum(r["foreign_net"] or 0 for r in inst_rows)
                tech["trust_net_60d"]   = sum(r["trust_net"]   or 0 for r in inst_rows)
                tech["foreign_net_10d"] = sum(r["foreign_net"] or 0 for r in inst_rows[:10])
                tech["trust_net_10d"]   = sum(r["trust_net"]   or 0 for r in inst_rows[:10])

            fund = calc_fundamentals(symbol, conn)
            # PE / PB
            pe_pb = calc_pe_pb(symbol, conn, close)
            fund.update(pe_pb)

            monthly = calc_monthly_revenue(symbol, conn)
            reasons, signal, score = apply_rules(tech, fund, close, monthly, market_win_rate)

            features = {**tech, **fund, **monthly}

            conn.execute(
                """INSERT OR REPLACE INTO recommendations
                   (symbol, date, score, signal, features_json, reasons_json, model_version, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    symbol, latest_date, score, signal,
                    json.dumps(features), json.dumps(reasons),
                    "rule_v3", int(time.time() * 1000)
                )
            )
            count += 1
            print(f"  {symbol}: score={score:.2f} signal={signal} reasons={reasons}", flush=True)

        except Exception as e:
            print(f"  [WARN] {symbol}: {e}", flush=True)

    conn.commit()

    # 寫入 sync_log
    conn.execute(
        "INSERT INTO sync_log (type, status, records_count, started_at, finished_at) VALUES (?,?,?,?,?)",
        ("analysis", "success", count, started_at, int(time.time() * 1000))
    )
    conn.commit()
    conn.close()

    print(f"\n規則引擎分析完成，共 {count} 檔", flush=True)


if __name__ == "__main__":
    run_rule_engine()
