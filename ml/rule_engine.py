"""
規則引擎：計算技術指標 + 基本面指標，產生推薦清單
改進：
1. base_score 底分從 0.45 降為對齊市場基準勝率（由 rule_scores.json 讀取）
2. 技術規則全部移出評分，只保留基本面規則決定 base_score
3. 大盤擇時：市場勝率 < 0.42 時提高門檻，> 0.55 時降低門檻
4. 籌碼邏輯加入絕對額度門檻，避免小量賣出誤判
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


def _load_rule_scores() -> tuple[dict, set, float]:
    """
    載入回測產生的規則分數。
    回傳 (scores_dict, suppressed_set, market_abs_win_rate)
    """
    if not RULE_SCORES_PATH.exists():
        return {}, set(), 0.45
    try:
        with open(RULE_SCORES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        scores = {}
        suppressed = set()
        mkt_baseline = data.get("market_abs_win_rate", 0.45)
        if mkt_baseline == 0.45:
            for v in data.get("rules", {}).values():
                if v.get("status") == "ok" and v.get("market_abs_win_rate") is not None:
                    mkt_baseline = v["market_abs_win_rate"]
                    break
        for rule, v in data.get("rules", {}).items():
            scores[rule] = v["score"]
            if v["status"] == "ok" and v.get("win_rate") is not None and v["win_rate"] < mkt_baseline:
                suppressed.add(rule)
        return scores, suppressed, float(mkt_baseline)
    except Exception:
        return {}, set(), 0.45


_RULE_SCORES, _SUPPRESSED_RULES, _BACKTEST_MKT_WIN_RATE = _load_rule_scores()


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

    rsi = ta.rsi(close, length=14)
    if rsi is not None and not rsi.empty:
        result["rsi14"] = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else None

    macd = ta.macd(close, fast=12, slow=26, signal=9)
    if macd is not None and not macd.empty:
        macd_val = macd.iloc[-1]
        result["macd"] = float(macd_val.get("MACD_12_26_9", 0) or 0)
        result["macd_signal"] = float(macd_val.get("MACDs_12_26_9", 0) or 0)
        result["macd_hist"] = float(macd_val.get("MACDh_12_26_9", 0) or 0)
        if len(macd) >= 2:
            prev = macd.iloc[-2]
            result["macd_hist_prev"] = float(prev.get("MACDh_12_26_9", 0) or 0)

    sma20 = ta.sma(close, length=20)
    sma60 = ta.sma(close, length=60)
    if sma20 is not None:
        result["sma20"] = float(sma20.iloc[-1]) if not pd.isna(sma20.iloc[-1]) else None
    if sma60 is not None and len(sma60.dropna()) > 0:
        result["sma60"] = float(sma60.iloc[-1]) if not pd.isna(sma60.iloc[-1]) else None

    bb = ta.bbands(close, length=20, std=2)
    if bb is not None and not bb.empty:
        bbu_col = next((c for c in bb.columns if c.startswith("BBU_")), None)
        bbl_col = next((c for c in bb.columns if c.startswith("BBL_")), None)
        if bbu_col:
            result["bb_upper"] = float(bb[bbu_col].iloc[-1] or 0)
        if bbl_col:
            result["bb_lower"] = float(bb[bbl_col].iloc[-1] or 0)

    vol = df["volume"]
    vol20 = vol.rolling(20).mean()
    if not pd.isna(vol20.iloc[-1]) and vol20.iloc[-1] > 0:
        result["vol_ratio"] = float(vol.iloc[-1] / vol20.iloc[-1])

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

    # 股數：用4季中位數減少配股/庫藏股雜訊
    shares_candidates = []
    for r in rows[:4]:
        if r["eps"] and r["eps"] != 0 and r["net_income"]:
            s = r["net_income"] / r["eps"]
            if s > 0:
                shares_candidates.append(s)
    shares = float(np.median(shares_candidates)) if shares_candidates else None

    eps_parts = []
    for r in rows[:4]:
        if r["eps"] is not None:
            eps_parts.append(r["eps"])
        elif r["net_income"] is not None and shares and shares > 0:
            eps_parts.append(r["net_income"] / shares)
    if eps_parts:
        result["eps_ttm"] = sum(eps_parts)

    ni_list = [r["net_income"] for r in rows[:4] if r["net_income"] is not None]
    if len(ni_list) >= 2:
        result["ni_ttm"] = sum(ni_list)

    equity_cur  = latest["equity"]
    equity_prev = rows[1]["equity"] if len(rows) > 1 else None
    if equity_cur and equity_cur > 0:
        result["equity"] = equity_cur

    # ROE：TTM淨利 / 平均equity
    if ni_list and equity_cur and equity_cur > 0:
        avg_equity = (equity_cur + equity_prev) / 2 if equity_prev and equity_prev > 0 else equity_cur
        result["roe"] = sum(ni_list) / avg_equity * 100

    if latest["total_assets"] and latest["total_assets"] > 0 and latest["total_debt"] is not None:
        result["debt_ratio"] = latest["total_debt"] / latest["total_assets"] * 100

    if rows[0]["revenue"]:
        result["revenue_abs"] = rows[0]["revenue"]
    if len(rows) >= 5 and rows[0]["revenue"] and rows[4]["revenue"] and rows[4]["revenue"] > 0:
        result["revenue_yoy"] = (rows[0]["revenue"] - rows[4]["revenue"]) / rows[4]["revenue"] * 100

    if len(rows) >= 5 and rows[0]["net_income"] and rows[4]["net_income"]:
        base_ni = rows[0]["net_income"]
        prev_ni = rows[4]["net_income"]
        if prev_ni > 0 and prev_ni > abs(base_ni) * 0.05:
            yoy = (base_ni - prev_ni) / prev_ni * 100
            if yoy <= 500:
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

    shares_candidates = []
    for r in rows:
        if r["eps"] and r["eps"] != 0 and r["net_income"]:
            s = r["net_income"] / r["eps"]
            if s > 0:
                shares_candidates.append(s)
    shares = float(np.median(shares_candidates)) if shares_candidates else None

    eps_ttm_parts = []
    for r in rows:
        if r["eps"] is not None:
            eps_ttm_parts.append(r["eps"])
        elif r["net_income"] is not None and shares and shares > 0:
            eps_ttm_parts.append(r["net_income"] / shares)

    eps_ttm = sum(eps_ttm_parts) if eps_ttm_parts else None
    # 負 EPS 時 PE 無意義
    if eps_ttm and eps_ttm > 0:
        result["pe_ratio"] = close / eps_ttm

    r0 = rows[0]
    if shares and r0["equity"] and r0["equity"] > 0:
        bvps = r0["equity"] / shares
        if bvps > 0:
            result["pb_ratio"] = close / bvps
    return result


def calc_monthly_revenue(symbol: str, conn) -> dict:
    rows = conn.execute(
        """SELECT year, month, revenue, yoy, mom
           FROM monthly_revenue WHERE symbol=?
           ORDER BY year DESC, month DESC LIMIT 12""",
        (symbol,)
    ).fetchall()

    if len(rows) < 2:
        return {}

    result = {}

    consecutive_yoy = 0
    for r in rows:
        if r["yoy"] is not None and r["yoy"] > 0:
            consecutive_yoy += 1
        else:
            break
    if consecutive_yoy > 0:
        result["rev_consecutive_yoy"] = consecutive_yoy

    consecutive_mom = 0
    for r in rows:
        if r["mom"] is not None and r["mom"] > 0:
            consecutive_mom += 1
        else:
            break
    if consecutive_mom > 0:
        result["rev_consecutive_mom"] = consecutive_mom

    if len(rows) >= 2 and rows[0]["yoy"] is not None and rows[1]["yoy"] is not None:
        if rows[0]["yoy"] > rows[1]["yoy"] > 0:
            result["rev_accel"] = True

    return result


def _calc_high_1y(df: pd.DataFrame) -> float | None:
    if "high" not in df.columns or "close" not in df.columns:
        return None
    recent = df.tail(250)
    close = recent["close"]
    ratios = close / close.shift(1)
    split_mask = ratios < 0.70
    if split_mask.any():
        last_split_loc = split_mask[split_mask].index[-1]
        recent = recent.loc[last_split_loc:]
    return float(recent["high"].max()) if not recent.empty else None


def _s(rule: str, fallback: float) -> float:
    return _RULE_SCORES.get(rule, fallback)


def apply_rules(tech: dict, fund: dict, close: float, monthly: dict | None = None,
                market_win_rate: float = 0.5) -> tuple[list[str], str, float]:
    """
    中長期選股邏輯。

    改進：
    1. base_score 底分對齊市場基準（用回測市場勝率，而非固定 0.45）
    2. 技術規則全部移出評分核心，只做輕微加/減分（±0.02），且被抑制時不加分
    3. 大盤擇時：市場勝率 < 0.42 為熊市，自動提高 buy/watch 門檻
    4. 籌碼加入絕對額度門檻（< 500張不計）避免小量干擾
    """
    reasons = []
    has_fundamental = False

    # ══ 前置過濾 ══
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

    # 短期漲太多過濾：20日漲超25% 且 RSI > 70 → 追高風險高，直接 neutral
    _r20 = tech.get("return20d")
    _rsi_early = tech.get("rsi14")
    if _r20 is not None and _r20 > 25 and _rsi_early is not None and _rsi_early > 70:
        return [f"⚠ 短期漲幅 {_r20:.0f}%（20日），RSI {_rsi_early:.0f}，追高風險"], "neutral", 0.3

    # ══ 第一層：基本面品質 → 決定 base_score ══
    # base_score 底分改為對齊回測市場基準勝率（熊市自動降低預設底分）
    base_floor = max(0.38, min(_BACKTEST_MKT_WIN_RATE, 0.48))
    base_score = base_floor

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

    base_score = min(base_score, 0.70)

    if not has_fundamental:
        return [], "neutral", 0.3

    # ══ 第二層：估值乘數 ══
    pe = fund.get("pe_ratio")
    pb = fund.get("pb_ratio")
    val_mult = 1.0

    if pe is not None:
        if pe > 40:
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

    # 短期漲幅過大懲罰（未達前置過濾門檻但仍偏貴）
    if _r20 is not None:
        if _r20 > 20:
            reasons.append(f"⚠ 近20日漲 {_r20:.0f}%")
            val_mult *= 0.82
        elif _r20 > 15:
            reasons.append(f"近20日漲 {_r20:.0f}%")
            val_mult *= 0.90

    val_mult = max(0.70, min(val_mult, 1.15))

    # ══ 第三層：月營收乘數 ══
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
                reasons.append(f"月營收月增連 {consecutive_mom} 個月")
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

    # ══ 第四層：籌碼乘數 ══
    # 加入絕對額度門檻（500張），避免小量法人操作誤判
    CHIP_MIN_ABS = 500  # 張
    foreign_60d = tech.get("foreign_net_60d") or tech.get("foreign_net_20d")
    trust_60d   = tech.get("trust_net_60d")   or tech.get("trust_net_20d")
    foreign_10d = tech.get("foreign_net_10d")
    trust_10d   = tech.get("trust_net_10d")

    chip_mult = 1.0
    if foreign_60d is not None and foreign_60d > CHIP_MIN_ABS:
        reasons.append(f"外資60日淨買 {foreign_60d/1e3:.0f}K")
        chip_mult *= 1.05
    if trust_60d is not None and trust_60d > CHIP_MIN_ABS:
        reasons.append(f"投信60日淨買 {trust_60d/1e3:.0f}K")
        chip_mult *= 1.05

    foreign_selling = foreign_10d is not None and foreign_10d < -CHIP_MIN_ABS
    foreign_buying  = foreign_10d is not None and foreign_10d > CHIP_MIN_ABS
    trust_selling   = trust_10d   is not None and trust_10d   < -CHIP_MIN_ABS
    trust_buying    = trust_10d   is not None and trust_10d   > CHIP_MIN_ABS
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

    # ══ 第五層：技術微調（±0.02，被抑制時不加分）══
    tech_adj = 0.0
    rsi = tech.get("rsi14")
    sma60 = tech.get("sma60")
    return20d = tech.get("return20d")

    if rsi is not None:
        if rsi < 35 and "rsi_oversold" not in _SUPPRESSED_RULES:
            reasons.append("RSI 低檔")
            tech_adj += 0.02
        elif rsi > 75:
            tech_adj -= 0.02

    if sma60 and close:
        if close >= sma60 * 0.95:
            tech_adj += 0.01
        else:
            tech_adj -= 0.02

    if return20d is not None and -10 < return20d < 0 and "pullback" not in _SUPPRESSED_RULES:
        reasons.append("近期回調")
        tech_adj += 0.01

    tech_adj = max(-0.04, min(tech_adj, 0.04))

    # ══ 最終分數 ══
    score = base_score * val_mult * rev_mult * chip_mult + tech_adj
    score = min(max(score, 0.0), 1.0)

    # ══ 動態門檻：大盤擇時 ══
    # market_win_rate < 0.42 → 熊市，提高門檻（減少誤判）
    # market_win_rate > 0.55 → 牛市，略降門檻
    # 基準：market_win_rate = 0.50 時，buy=0.56, watch=0.50
    buy_thresh   = 0.56 + (market_win_rate - 0.50) * 0.30
    watch_thresh = 0.50 + (market_win_rate - 0.50) * 0.30
    # 熊市下限：buy 至少 0.58，watch 至少 0.52
    if market_win_rate < 0.42:
        buy_thresh   = max(buy_thresh, 0.58)
        watch_thresh = max(watch_thresh, 0.52)
    buy_thresh   = max(0.52, min(buy_thresh,   0.65))
    watch_thresh = max(0.46, min(watch_thresh, 0.58))

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
                if abs(ret) <= 0.5:
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

    row = conn.execute(
        "SELECT date FROM stock_prices ORDER BY date DESC LIMIT 1"
    ).fetchone()
    if not row:
        print("無價格資料，請先執行 sync.py prices", flush=True)
        return
    latest_date = row["date"]
    print(f"分析日期：{latest_date}", flush=True)

    market_win_rate = _calc_market_win_rate(conn)
    market_env = "熊市" if market_win_rate < 0.42 else ("牛市" if market_win_rate > 0.55 else "正常")
    print(f"市場近期勝率：{market_win_rate:.1%}（{market_env}，動態門檻基準）", flush=True)

    symbols = [r["symbol"] for r in conn.execute("SELECT symbol FROM stocks").fetchall()]
    count = 0

    for symbol in symbols:
        try:
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

            latest_price_row = conn.execute(
                "SELECT close, volume FROM stock_prices WHERE symbol=? ORDER BY date DESC LIMIT 1",
                (symbol,)
            ).fetchone()
            if not latest_price_row:
                continue

            close = latest_price_row["close"]

            tech = calc_indicators(df)
            if not tech:
                continue

            tech["high_1y"] = _calc_high_1y(df)

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
                    "rule_v4", int(time.time() * 1000)
                )
            )
            count += 1
            print(f"  {symbol}: score={score:.2f} signal={signal} reasons={reasons}", flush=True)

        except Exception as e:
            print(f"  [WARN] {symbol}: {e}", flush=True)

    conn.commit()
    conn.execute(
        "INSERT INTO sync_log (type, status, records_count, started_at, finished_at) VALUES (?,?,?,?,?)",
        ("analysis", "success", count, started_at, int(time.time() * 1000))
    )
    conn.commit()
    conn.close()

    print(f"\n規則引擎分析完成，共 {count} 檔", flush=True)


if __name__ == "__main__":
    run_rule_engine()
