"""
規則引擎：計算技術指標 + 基本面指標，產生推薦清單
用法：python ml/rule_engine.py

評分架構（修正版）：
- base = 底分（市場基準）+ 基本面超額勝率遞減加成
- × 估值乘數 × 月營收乘數 × 籌碼乘數
- + 技術面微調 ±0.04
- 動態門檻依大盤環境調整

籌碼單位：DB 存的是「股」，500張 = 500,000股
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

sys.path.insert(0, str(Path(__file__).parent))
from fundamentals import calc_fundamentals
from strategies import calc_piotroski, calc_peg, calc_minervini
from agents import apply_agents

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
            status = v.get("status", "")
            # 抑制：勝率低於市場基準 或 樣本不足（low_confidence）
            if status in ("ok", "low_confidence") and v.get("win_rate") is not None and v["win_rate"] < mkt_baseline:
                suppressed.add(rule)
            if status == "low_confidence":
                # 低信心結果不完全採用，向 fallback 靠攏（已在 backtest 端處理）
                pass
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

    # Minervini 趨勢模板所需均線
    sma50 = ta.sma(close, length=50)
    sma150 = ta.sma(close, length=150)
    sma200 = ta.sma(close, length=200)
    if sma50 is not None and len(sma50.dropna()) > 0:
        result["sma50"] = float(sma50.iloc[-1]) if not pd.isna(sma50.iloc[-1]) else None
    if sma150 is not None and len(sma150.dropna()) > 0:
        result["sma150"] = float(sma150.iloc[-1]) if not pd.isna(sma150.iloc[-1]) else None
    if sma200 is not None and len(sma200.dropna()) > 0:
        result["sma200"] = float(sma200.iloc[-1]) if not pd.isna(sma200.iloc[-1]) else None
        # SMA200 一個月前的值（判斷是否上升趨勢）
        if len(sma200.dropna()) >= 22:
            result["sma200_1m_ago"] = float(sma200.dropna().iloc[-22])

    # 52 週低點
    if len(close) >= 250:
        result["low_1y"] = float(df["low"].tail(250).min())
    elif len(close) >= 60:
        result["low_1y"] = float(df["low"].min())

    # 60 日低點（資金離場偵測用）
    if len(close) >= 60:
        result["low_60d"] = float(df["low"].tail(60).min())

    # 12-1 月動量（CANSLIM RS 用，跳過最近 1 個月避免短期反轉）
    if len(close) >= 252:
        result["momentum_12_1"] = float((close.iloc[-22] / close.iloc[-252] - 1) * 100)
    elif len(close) >= 126:
        result["momentum_12_1"] = float((close.iloc[-22] / close.iloc[0] - 1) * 100)

    # 量價背離偵測（近20日）
    if len(close) >= 21 and len(vol) >= 21:
        price_up = close.iloc[-1] > close.iloc[-21]
        vol_down = vol.iloc[-5:].mean() < vol.iloc[-21:-5].mean() * 0.7
        price_down = close.iloc[-1] < close.iloc[-21]
        vol_up = vol.iloc[-5:].mean() > vol.iloc[-21:-5].mean() * 1.5
        if price_up and vol_down:
            result["vol_price_divergence"] = "bearish"  # 價漲量縮
        elif price_down and vol_up:
            result["vol_price_divergence"] = "bullish"  # 價跌量增（可能是洗盤）

    result["close"] = float(close.iloc[-1])
    result["volume"] = int(df["volume"].iloc[-1])

    return result


def calc_monthly_revenue(symbol: str, conn) -> dict:
    """計算營收連續成長指標。優先用月營收表，無資料時 fallback 到季營收。"""
    # 嘗試月營收
    rows = conn.execute(
        """SELECT year, month, revenue, yoy, mom
           FROM monthly_revenue WHERE symbol=?
           ORDER BY year DESC, month DESC LIMIT 12""",
        (symbol,)
    ).fetchall()

    if len(rows) >= 2:
        return _calc_monthly_rev_indicators(rows)

    # Fallback：用季營收（financials 表）計算連續 YoY 成長
    return _calc_quarterly_rev_indicators(symbol, conn)


def _calc_monthly_rev_indicators(rows) -> dict:
    """從月營收資料計算指標"""
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


def _calc_quarterly_rev_indicators(symbol: str, conn) -> dict:
    """從季營收（financials）計算連續 YoY 成長（月營收的替代方案）"""
    rows = conn.execute(
        """SELECT year, quarter, revenue
           FROM financials WHERE symbol=? AND revenue IS NOT NULL AND revenue > 0
           ORDER BY year DESC, quarter DESC LIMIT 8""",
        (symbol,)
    ).fetchall()

    if len(rows) < 5:
        return {}

    result = {}

    # 計算每季 YoY（當季 vs 去年同季）
    # rows[0]=最新, rows[4]=去年同季, rows[1] vs rows[5], ...
    yoy_list = []
    for i in range(min(4, len(rows) - 4)):
        cur = rows[i]["revenue"]
        prev = rows[i + 4]["revenue"] if (i + 4) < len(rows) else None
        if cur and prev and prev > 0:
            yoy_list.append((cur - prev) / prev * 100)
        else:
            yoy_list.append(None)

    # 連續季營收 YoY > 0
    consecutive = 0
    for y in yoy_list:
        if y is not None and y > 0:
            consecutive += 1
        else:
            break

    if consecutive > 0:
        # 季度轉月度近似：1季 ≈ 3個月
        result["rev_consecutive_yoy"] = consecutive * 3

    # 加速成長：最新季 YoY > 前一季 YoY > 0
    if len(yoy_list) >= 2 and yoy_list[0] is not None and yoy_list[1] is not None:
        if yoy_list[0] > yoy_list[1] > 0:
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


def _excess_win(rule: str, fallback: float) -> float:
    """
    規則的超額勝率（rule win_rate - market win_rate）。
    這才是規則真正的 alpha，避免 raw win_rate 直接當分數加總導致飽和。
    """
    raw = _RULE_SCORES.get(rule)
    if raw is not None:
        return max(0.0, raw - _BACKTEST_MKT_WIN_RATE)
    return fallback


def apply_rules(tech: dict, fund: dict, close: float, monthly: dict | None = None,
                market_win_rate: float = 0.5,
                piotroski: dict | None = None, peg_data: dict | None = None,
                minervini: dict | None = None, rs_pctile: float | None = None,
                industry_median_pe: float | None = None,
                ) -> tuple[list[str], str, float]:
    """
    中長期選股邏輯。

    評分架構（v5 加減分制）：
    - score = 0.40（底分）
    - + 基本面加分（ROE/營收/獲利/Piotroski，遞減加成）
    - + 估值加減分（PE/PB/PEG/殖利率）
    - + 月營收加分
    - + 籌碼加減分（法人買賣超，警告不重扣）
    - + 技術面加減分（趨勢/RSI/回調）
    - 動態門檻依大盤環境調整
    """
    reasons = []
    has_fundamental = False

    # == 前置過濾（硬性排除）==
    high_1y = tech.get("high_1y")
    if high_1y is not None and high_1y > 0 and close > 0:
        drawdown = (close - high_1y) / high_1y
        if drawdown < -0.6:
            return [f"⚠ 近一年跌幅 {drawdown*100:.0f}%"], "neutral", 0.20

    roe_check = fund.get("roe")
    ni_ttm_check = fund.get("ni_ttm")
    if roe_check is not None and roe_check < 0:
        return [], "neutral", 0.20
    if ni_ttm_check is not None and ni_ttm_check < 0:
        return [], "neutral", 0.20

    _pe_early = fund.get("pe_ratio")
    if _pe_early is not None and _pe_early > 60:
        return [f"⚠ PE {_pe_early:.0f} 過高"], "neutral", 0.20

    # 新增：近期弱勢前置過濾
    # 條件 A：return60d < -20% 且均線空頭排列 → 強制 neutral
    # 條件 B：長期均線空頭排列（MA20<MA60<MA150<MA200）→ 強制 neutral（股價在大趨勢下跌中，不追基本面）
    _r60 = tech.get("return60d")
    _sma20 = tech.get("sma20")
    _sma60 = tech.get("sma60")
    _sma150 = tech.get("sma150")
    _sma200 = tech.get("sma200")
    short_bearish = _sma20 is not None and _sma60 is not None and _sma150 is not None \
                    and _sma20 < _sma60 < _sma150
    long_bearish = short_bearish and _sma200 is not None and _sma150 < _sma200
    if _r60 is not None and _r60 < -20 and short_bearish:
        return [f"⚠ 近60日跌 {_r60:.0f}% + 均線空頭排列"], "neutral", 0.25
    if long_bearish:
        return ["⚠ 均線完全空頭排列（MA20<MA60<MA150<MA200）"], "neutral", 0.25

    # == 第一層：基本面品質 → 加分 ==
    score = 0.40  # 底分

    fund_signals: list[tuple[str, float]] = []

    roe = fund.get("roe")
    if roe is not None:
        if roe >= 20:
            reasons.append(f"ROE {roe:.1f}%")
            fund_signals.append(("roe_high", _excess_win("roe_high", 0.08)))
            has_fundamental = True
        elif roe >= 12:
            reasons.append(f"ROE {roe:.1f}%")
            fund_signals.append(("roe_ok", _excess_win("roe_ok", 0.05)))
            has_fundamental = True

    revenue_yoy = fund.get("revenue_yoy")
    revenue_abs = fund.get("revenue_abs")
    if revenue_yoy is not None and revenue_yoy > 5 and revenue_abs is not None and revenue_abs >= 1e8:
        reasons.append(f"營收 YoY +{revenue_yoy:.0f}%")
        fund_signals.append(("revenue_yoy", _excess_win("revenue_yoy", 0.04)))
        has_fundamental = True

    ni_yoy = fund.get("ni_yoy")
    if ni_yoy is not None and ni_yoy > 10:
        reasons.append(f"獲利 YoY +{ni_yoy:.0f}%")
        fund_signals.append(("ni_yoy", _excess_win("ni_yoy", 0.04)))
        has_fundamental = True

    debt_ratio = fund.get("debt_ratio")
    if debt_ratio is not None and debt_ratio < 50:
        fund_signals.append(("debt_low", _excess_win("debt_low", 0.02)))

    if piotroski:
        f_score = piotroski.get("piotroski", 0)
        if f_score >= 5:
            reasons.append(f"Piotroski {f_score}/6")
            fund_signals.append(("piotroski_high", _excess_win("piotroski_high", 0.06)))
            has_fundamental = True
        elif f_score >= 4:
            reasons.append(f"Piotroski {f_score}/6")
            fund_signals.append(("piotroski_ok", _excess_win("piotroski_ok", 0.03)))
        elif f_score <= 1:
            return [f"⚠ Piotroski {f_score}/6 品質極差"], "neutral", 0.20

    if not has_fundamental:
        return [], "neutral", 0.20

    # 遞減加成
    fund_signals.sort(key=lambda x: x[1], reverse=True)
    decay_weights = [1.0, 0.5, 0.3, 0.15, 0.1]
    for i, (rule_name, excess) in enumerate(fund_signals):
        if rule_name in _SUPPRESSED_RULES:
            continue
        w = decay_weights[i] if i < len(decay_weights) else 0.1
        score += excess * w

    # == 第二層：估值加減分 ==
    pe = fund.get("pe_ratio")
    pb = fund.get("pb_ratio")

    if pe is not None:
        if pe > 40:
            score -= 0.06
        elif pe > 30:
            score -= 0.03
        elif pe < 15:
            reasons.append(f"低本益比 PE {pe:.0f}")
            score += 0.04
        elif pe <= 25:
            reasons.append(f"PE {pe:.0f}")
            score += 0.02

    if pb is not None:
        if pb < 2:
            reasons.append(f"PB {pb:.1f}")
            score += 0.02
        elif pb > 4:
            score -= 0.02

    if peg_data:
        peg = peg_data.get("peg")
        eps_g = peg_data.get("eps_growth")
        if peg is not None and eps_g is not None and eps_g >= 10:
            if peg < 1.0:
                reasons.append(f"PEG {peg:.1f}（成長價值）")
                score += 0.04
            elif peg < 1.5:
                reasons.append(f"PEG {peg:.1f}")
                score += 0.02
            elif peg > 2.5:
                score -= 0.02

    div_yield = fund.get("div_yield")
    if div_yield is not None:
        if div_yield >= 6:
            reasons.append(f"高殖利率 {div_yield:.1f}%")
            score += 0.03
        elif div_yield >= 4:
            reasons.append(f"殖利率 {div_yield:.1f}%")
            score += 0.02

    if pe is not None and industry_median_pe is not None and industry_median_pe > 0:
        pe_relative = pe / industry_median_pe
        if pe_relative < 0.7:
            reasons.append(f"產業低估 PE={pe:.0f} (同業中位 {industry_median_pe:.0f})")
            score += 0.03
        elif pe_relative > 1.5:
            reasons.append(f"⚠ 產業高估 PE={pe:.0f} (同業中位 {industry_median_pe:.0f})")
            score -= 0.03

    # 追高風險：標警告 + 輕扣分（不再重扣）
    _r20 = tech.get("return20d")
    if _r20 is not None:
        if _r20 > 30:
            reasons.append(f"⚠ 追高風險：近20日漲 {_r20:.0f}%")
            score -= 0.04
        elif _r20 > 20:
            reasons.append(f"⚠ 追高風險：近20日漲 {_r20:.0f}%")
            score -= 0.03
        elif _r20 > 15:
            reasons.append(f"⚠ 短期偏熱：近20日漲 {_r20:.0f}%")
            score -= 0.01

    # == 第三層：月營收加分 ==
    if monthly:
        consecutive_yoy = monthly.get("rev_consecutive_yoy", 0)
        consecutive_mom = monthly.get("rev_consecutive_mom", 0)
        rev_accel = monthly.get("rev_accel", False)

        if consecutive_yoy >= 6:
            reasons.append(f"月營收年增連 {consecutive_yoy} 個月")
            score += 0.05
            has_fundamental = True
            if consecutive_mom >= 3:
                reasons.append(f"月營收月增連 {consecutive_mom} 個月")
            if rev_accel:
                reasons.append("月營收成長加速")
                score += 0.02
        elif consecutive_yoy >= 3:
            reasons.append(f"月營收年增連 {consecutive_yoy} 個月")
            score += 0.03
            has_fundamental = True
            if rev_accel:
                reasons.append("月營收成長加速")
                score += 0.01
        elif consecutive_mom >= 3:
            reasons.append(f"月營收月增連 {consecutive_mom} 個月")
            score += 0.02
        elif rev_accel:
            reasons.append("月營收成長加速")
            score += 0.01

    # == 第四層：籌碼加減分（警告不重扣）==
    CHIP_MIN_ABS = 500_000  # 股（= 500 張）
    foreign_60d = tech.get("foreign_net_60d") or tech.get("foreign_net_20d")
    trust_60d   = tech.get("trust_net_60d")   or tech.get("trust_net_20d")
    foreign_10d = tech.get("foreign_net_10d")
    trust_10d   = tech.get("trust_net_10d")

    if foreign_60d is not None and foreign_60d > CHIP_MIN_ABS:
        reasons.append(f"外資60日淨買 {foreign_60d/1000:,.0f}張")
        score += 0.03
    if trust_60d is not None and trust_60d > CHIP_MIN_ABS:
        reasons.append(f"投信60日淨買 {trust_60d/1000:,.0f}張")
        score += 0.03

    foreign_selling = foreign_10d is not None and foreign_10d < -CHIP_MIN_ABS
    foreign_buying  = foreign_10d is not None and foreign_10d > CHIP_MIN_ABS
    trust_selling   = trust_10d   is not None and trust_10d   < -CHIP_MIN_ABS
    trust_buying    = trust_10d   is not None and trust_10d   > CHIP_MIN_ABS
    chip_warning    = foreign_selling and trust_selling

    def _chip_str(val):
        v = abs(val) / 1000
        return f"{v:,.0f}張"

    # 雙引擎：外資+投信 10 日同步買超（短線最強訊號）
    if foreign_buying and trust_buying:
        reasons.append(f"外資+投信10日雙買 +{_chip_str(foreign_10d)}／+{_chip_str(trust_10d)}")
        score += 0.05
    elif foreign_buying and not trust_selling:
        # 外資 10 日單邊買超（投信中性）
        f_abs = abs(foreign_10d)
        if f_abs > CHIP_MIN_ABS * 2:  # >1000 張
            reasons.append(f"外資10日買超 +{_chip_str(foreign_10d)}")
            score += 0.03
        else:
            reasons.append(f"外資10日買超 +{_chip_str(foreign_10d)}")
            score += 0.02
    elif trust_buying and not foreign_selling:
        # 投信 10 日單邊買超（外資中性）
        reasons.append(f"投信10日買超 +{_chip_str(trust_10d)}")
        score += 0.03
    elif foreign_buying and trust_selling:
        f, t = abs(foreign_10d), abs(trust_10d)
        if t > f * 3:
            reasons.append(f"⚠ 投信大賣 -{_chip_str(trust_10d)}／外資小買 +{_chip_str(foreign_10d)}（近10日）")
            score -= 0.02
        else:
            reasons.append(f"外資買超 +{_chip_str(foreign_10d)}／投信賣超 -{_chip_str(trust_10d)}（近10日）")
    elif foreign_selling and trust_buying:
        f, t = abs(foreign_10d), abs(trust_10d)
        if f > t * 3:
            reasons.append(f"⚠ 外資大賣 -{_chip_str(foreign_10d)}／投信小買 +{_chip_str(trust_10d)}（近10日）")
            score -= 0.02
        else:
            reasons.append(f"投信買超 +{_chip_str(trust_10d)}／外資賣超 -{_chip_str(foreign_10d)}（近10日）")

    if chip_warning:
        reasons.append(f"⚠ 法人近10日同步賣超（外資 -{_chip_str(foreign_10d)} 投信 -{_chip_str(trust_10d)}）")
        score -= 0.03

    # 連續買賣超（即使 60 日淨買，近期連續賣也要提醒）
    f_consec_sell = tech.get("foreign_consec_sell", 0)
    t_consec_sell = tech.get("trust_consec_sell", 0)
    f_consec_buy = tech.get("foreign_consec_buy", 0)
    t_consec_buy = tech.get("trust_consec_buy", 0)

    if f_consec_sell >= 5:
        reasons.append(f"⚠ 外資連續賣超 {f_consec_sell} 日")
        score -= 0.02
    elif f_consec_sell >= 3:
        reasons.append(f"⚠ 外資連續賣超 {f_consec_sell} 日")
        score -= 0.01
    elif f_consec_buy >= 5:
        reasons.append(f"外資連續買超 {f_consec_buy} 日")
        score += 0.04
    elif f_consec_buy >= 3:
        reasons.append(f"外資連續買超 {f_consec_buy} 日")
        score += 0.02

    if t_consec_sell >= 5:
        reasons.append(f"⚠ 投信連續賣超 {t_consec_sell} 日")
        score -= 0.02
    elif t_consec_sell >= 3:
        reasons.append(f"⚠ 投信連續賣超 {t_consec_sell} 日")
        score -= 0.01
    elif t_consec_buy >= 5:
        reasons.append(f"投信連續買超 {t_consec_buy} 日")
        score += 0.04
    elif t_consec_buy >= 3:
        reasons.append(f"投信連續買超 {t_consec_buy} 日")
        score += 0.02

    dealer_10d = tech.get("dealer_net_10d")
    if dealer_10d is not None and abs(dealer_10d) > CHIP_MIN_ABS:
        if dealer_10d > CHIP_MIN_ABS * 5:
            reasons.append(f"⚠ 自營商大買 +{_chip_str(dealer_10d)}（近10日，反指標）")
            score -= 0.01

    margin_chg = tech.get("margin_balance_chg_10d")
    short_balance = tech.get("short_balance")
    if margin_chg is not None:
        if margin_chg > 15:
            reasons.append(f"⚠ 融資大增 +{margin_chg:.0f}%（散戶追高）")
            score -= 0.02
        elif margin_chg < -15:
            reasons.append(f"融資大減 {margin_chg:.0f}%（籌碼沉澱）")
            score += 0.01

    if short_balance is not None and short_balance > 0:
        vol = tech.get("volume", 0)
        if vol > 0:
            short_ratio = short_balance / vol
            if short_ratio > 5:
                reasons.append(f"融券/量比 {short_ratio:.1f} 天（軋空壓力）")
                score += 0.01

    # == 第五層：技術面加減分 ==
    rsi = tech.get("rsi14")
    return20d = tech.get("return20d")

    if minervini:
        m_score = minervini.get("minervini", 0)
        if m_score >= 7:
            reasons.append(f"趨勢強勁 {m_score}/8 (Minervini)")
            score += 0.03
        elif m_score >= 5:
            reasons.append(f"趨勢健康 {m_score}/8")
            score += 0.02
        elif m_score <= 2:
            reasons.append(f"⚠ 趨勢疲弱 {m_score}/8")
            score -= 0.05
        elif m_score <= 4:
            score -= 0.02
    else:
        sma60 = tech.get("sma60")
        if sma60 and close:
            if close >= sma60 * 0.95:
                score += 0.01
            elif close < sma60 * 0.85:
                reasons.append("⚠ 跌破 60 日均線 15%")
                score -= 0.05
            else:
                score -= 0.02

    # 60 日新低 + 量縮（資金離場）→ 扣分
    low_60d = tech.get("low_60d")
    vol_ratio = tech.get("vol_ratio")
    if low_60d is not None and close <= low_60d * 1.02 and vol_ratio is not None and vol_ratio < 0.7:
        reasons.append("⚠ 60 日低點 + 量縮（資金離場）")
        score -= 0.05

    # 短期均線空頭排列（MA20<MA60<MA150）且基本面好 → 避免追加，額外扣分
    _sma20_chk = tech.get("sma20")
    _sma60_chk = tech.get("sma60")
    _sma150_chk = tech.get("sma150")
    if _sma20_chk and _sma60_chk and _sma150_chk and _sma20_chk < _sma60_chk < _sma150_chk:
        reasons.append("⚠ 均線空頭排列（MA20<MA60<MA150）")
        score -= 0.06

    if rs_pctile is not None:
        if rs_pctile >= 90:
            reasons.append(f"相對強度 RS {rs_pctile:.0f}%")
            score += 0.03
        elif rs_pctile >= 80:
            reasons.append(f"相對強度 RS {rs_pctile:.0f}%")
            score += 0.02
        elif rs_pctile <= 20:
            score -= 0.02

    if rsi is not None:
        if rsi < 35 and "rsi_oversold" not in _SUPPRESSED_RULES:
            reasons.append("RSI 低檔")
            score += 0.01
        elif rsi > 75:
            score -= 0.01

    if return20d is not None and -10 < return20d < 0 and "pullback" not in _SUPPRESSED_RULES:
        reasons.append("近期回調")
        score += 0.01

    vpd = tech.get("vol_price_divergence")
    if vpd == "bearish":
        reasons.append("⚠ 價漲量縮（量價背離）")
        score -= 0.02
    elif vpd == "bullish":
        reasons.append("價跌量增（可能洗盤）")
        score += 0.01

    # == 最終分數 ==
    score = min(max(score, 0.0), 1.0)

    # == 動態門檻 ==
    buy_thresh   = 0.56 + (market_win_rate - 0.50) * 0.30
    watch_thresh = 0.50 + (market_win_rate - 0.50) * 0.30
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

    symbols = [r["symbol"] for r in conn.execute("SELECT symbol FROM stocks WHERE market='TSE'").fetchall()]

    # == Pass 1：計算所有股票的 12-1 月動量，建立 RS 排名 ==
    from scipy.stats import percentileofscore
    print("Pass 1：計算相對強度排名...", flush=True)
    momentum_map = {}
    for symbol in symbols:
        price_rows = conn.execute(
            "SELECT close FROM stock_prices WHERE symbol=? ORDER BY date DESC LIMIT 260",
            (symbol,)
        ).fetchall()
        if len(price_rows) >= 126:
            closes = [r["close"] for r in reversed(price_rows)]
            # 12-1 month: skip last 22 days, look back to ~252 days
            end_idx = max(0, len(closes) - 22)
            start_idx = 0
            if end_idx > 0 and closes[start_idx] > 0:
                momentum_map[symbol] = (closes[end_idx] / closes[start_idx] - 1) * 100

    all_moms = sorted(momentum_map.values()) if momentum_map else []
    rs_map = {}
    if all_moms:
        for sym, mom in momentum_map.items():
            rs_map[sym] = float(percentileofscore(all_moms, mom, kind='rank'))
    print(f"  RS 排名完成：{len(rs_map)} 檔", flush=True)

    # == Pass 1b：建立產業 PE 中位數（用 stock_tags 的 sub_tag 分群）==
    print("Pass 1b：計算產業 PE 中位數...", flush=True)
    industry_pe_map: dict[str, float] = {}  # symbol -> industry median PE
    # 用 stock_tags 的第一個 sub_tag 做產業分群
    tag_rows = conn.execute("SELECT symbol, sub_tag FROM stock_tags").fetchall()
    symbol_industry: dict[str, str] = {}
    for tr in tag_rows:
        if tr["symbol"] not in symbol_industry:
            symbol_industry[tr["symbol"]] = tr["sub_tag"]
    # 收集每個產業的 PE
    from collections import defaultdict
    industry_pes: dict[str, list] = defaultdict(list)
    for sym in symbols:
        ind = symbol_industry.get(sym)
        if not ind:
            continue
        pe_row = conn.execute(
            """SELECT r.features_json FROM recommendations r
               WHERE r.symbol=? ORDER BY r.date DESC LIMIT 1""",
            (sym,)
        ).fetchone()
        if pe_row and pe_row["features_json"]:
            feats = json.loads(pe_row["features_json"])
            pe_val = feats.get("pe_ratio")
            if pe_val and 0 < pe_val < 200:
                industry_pes[ind].append(pe_val)
    for ind, pes in industry_pes.items():
        if len(pes) >= 3:
            med = float(np.median(pes))
            for sym, s_ind in symbol_industry.items():
                if s_ind == ind:
                    industry_pe_map[sym] = med
    print(f"  產業 PE 中位數：{len(industry_pes)} 個產業，{len(industry_pe_map)} 檔覆蓋", flush=True)

    # == Pass 2：逐股分析 ==
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
                "SELECT foreign_net, trust_net, dealer_net FROM institutional WHERE symbol=? ORDER BY date DESC LIMIT 60",
                (symbol,)
            ).fetchall()
            if inst_rows:
                tech["foreign_net_60d"] = sum(r["foreign_net"] or 0 for r in inst_rows)
                tech["trust_net_60d"]   = sum(r["trust_net"]   or 0 for r in inst_rows)
                tech["foreign_net_10d"] = sum(r["foreign_net"] or 0 for r in inst_rows[:10])
                tech["trust_net_10d"]   = sum(r["trust_net"]   or 0 for r in inst_rows[:10])
                tech["dealer_net_10d"]  = sum(r["dealer_net"]  or 0 for r in inst_rows[:10])
                # 連續買賣超天數
                f_consec_sell, t_consec_sell = 0, 0
                f_consec_buy, t_consec_buy = 0, 0
                for r in inst_rows:
                    if (r["foreign_net"] or 0) < 0:
                        f_consec_sell += 1
                    else:
                        break
                for r in inst_rows:
                    if (r["foreign_net"] or 0) > 0:
                        f_consec_buy += 1
                    else:
                        break
                for r in inst_rows:
                    if (r["trust_net"] or 0) < 0:
                        t_consec_sell += 1
                    else:
                        break
                for r in inst_rows:
                    if (r["trust_net"] or 0) > 0:
                        t_consec_buy += 1
                    else:
                        break
                tech["foreign_consec_sell"] = f_consec_sell
                tech["trust_consec_sell"] = t_consec_sell
                tech["foreign_consec_buy"] = f_consec_buy
                tech["trust_consec_buy"] = t_consec_buy

            # 融資融券
            margin_rows = conn.execute(
                "SELECT margin_balance, short_balance FROM margin_trading WHERE symbol=? ORDER BY date DESC LIMIT 15",
                (symbol,)
            ).fetchall()
            if margin_rows and len(margin_rows) >= 11:
                mb_now = margin_rows[0]["margin_balance"] or 0
                mb_10  = margin_rows[10]["margin_balance"] or 0
                if mb_10 > 0:
                    tech["margin_balance_chg_10d"] = (mb_now - mb_10) / mb_10 * 100
                tech["short_balance"] = margin_rows[0]["short_balance"] or 0

            fund = calc_fundamentals(symbol, conn, price=close)

            # 新策略指標
            pio = calc_piotroski(symbol, conn)
            peg = calc_peg(fund)
            mini = calc_minervini(tech, close)
            rs = rs_map.get(symbol)

            monthly = calc_monthly_revenue(symbol, conn)
            ind_pe = industry_pe_map.get(symbol)
            reasons, signal, score = apply_rules(
                tech, fund, close, monthly, market_win_rate,
                piotroski=pio, peg_data=peg, minervini=mini, rs_pctile=rs,
                industry_median_pe=ind_pe,
            )

            # 第六層：大師共識軟加分
            tag_rows = conn.execute(
                "SELECT tag, sub_tag FROM stock_tags WHERE symbol=?", (symbol,)
            ).fetchall()
            agent_ctx = {
                "fund": fund, "tech": tech, "monthly": monthly,
                "minervini": mini, "rs_pctile": rs,
                "tags": [{"tag": t["tag"], "sub_tag": t["sub_tag"]} for t in tag_rows],
            }
            agent_result = apply_agents(agent_ctx)
            score = max(0.0, min(1.0, score + agent_result["bonus"]))
            bullish_n = agent_result["consensus"]["bullish"]
            if bullish_n >= 5:
                reasons.append(f"大師共識 {bullish_n}/7 看多")
            elif agent_result["consensus"]["bearish"] >= 5:
                reasons.append(f"大師共識 {agent_result['consensus']['bearish']}/7 看空")

            features = {**tech, **fund, **monthly}
            if pio:
                features["piotroski"] = pio.get("piotroski")
            if peg:
                features["peg"] = peg.get("peg")
            if mini:
                features["minervini"] = mini.get("minervini")
            if rs is not None:
                features["rs_pctile"] = round(rs, 1)
            features["agent_score"] = agent_result["agent_score"]
            features["agent_consensus"] = agent_result["consensus"]
            features["agent_details"] = agent_result["details"]

            conn.execute(
                """INSERT OR REPLACE INTO recommendations
                   (symbol, date, score, signal, features_json, reasons_json, model_version, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    symbol, latest_date, score, signal,
                    json.dumps(features), json.dumps(reasons),
                    "rule_v6", int(time.time() * 1000)
                )
            )
            count += 1
            try:
                print(f"  {symbol}: score={score:.2f} signal={signal} reasons={reasons}", flush=True)
            except UnicodeEncodeError:
                print(f"  {symbol}: score={score:.2f} signal={signal} reasons=({len(reasons)} items)", flush=True)

        except Exception as e:
            try:
                print(f"  [WARN] {symbol}: {e}", flush=True)
            except UnicodeEncodeError:
                print(f"  [WARN] {symbol}: (encoding error)", flush=True)

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
