"""規則引擎測試"""
import sys
import sqlite3
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from rule_engine import calc_indicators, apply_rules, calc_monthly_revenue, _calc_high_1y
from fundamentals import calc_fundamentals

DB_PATH = Path(__file__).parent.parent.parent / "data" / "stock.db"


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _load_df(symbol="2330.TW"):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume FROM stock_prices WHERE symbol=? ORDER BY date ASC",
        (symbol,)
    ).fetchall()
    conn.close()
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    return df


def test_calc_indicators_has_required_keys():
    """calc_indicators 應回傳必要的技術指標"""
    df = _load_df()
    tech = calc_indicators(df)
    assert "rsi14" in tech
    assert "close" in tech
    assert "volume" in tech
    assert "sma20" in tech


def test_calc_indicators_rsi_range():
    """RSI 應在 0~100"""
    df = _load_df()
    tech = calc_indicators(df)
    if tech.get("rsi14") is not None:
        assert 0 <= tech["rsi14"] <= 100


def test_apply_rules_returns_tuple():
    """apply_rules 應回傳 (reasons, signal, score) 三元組"""
    df = _load_df()
    tech = calc_indicators(df)
    conn = _get_conn()
    fund = calc_fundamentals("2330.TW", conn, price=float(df["close"].iloc[-1]))
    monthly = calc_monthly_revenue("2330.TW", conn)
    conn.close()

    result = apply_rules(tech, fund, float(df["close"].iloc[-1]), monthly)
    assert isinstance(result, tuple)
    assert len(result) == 3
    reasons, signal, score = result
    assert isinstance(reasons, list)
    assert signal in ("buy", "watch", "neutral")
    assert 0.0 <= score <= 1.0


def test_apply_rules_negative_roe_neutral():
    """ROE < 0 應直接 neutral"""
    fund = {"roe": -5.0}
    reasons, signal, score = apply_rules({}, fund, 100.0)
    assert signal == "neutral"


def test_apply_rules_high_pe_neutral():
    """PE > 60 應直接 neutral"""
    fund = {"pe_ratio": 80.0, "roe": 15.0}
    reasons, signal, score = apply_rules({}, fund, 100.0)
    assert signal == "neutral"


def test_apply_rules_no_fundamental_neutral():
    """沒有基本面訊號應 neutral"""
    fund = {"roe": 5.0, "debt_ratio": 60.0}  # ROE 不夠高，debt 不夠低
    reasons, signal, score = apply_rules({}, fund, 100.0)
    assert signal == "neutral"


def test_apply_rules_score_capped():
    """分數不應超過 1.0"""
    fund = {"roe": 30.0, "revenue_yoy": 50.0, "ni_yoy": 100.0, "revenue_abs": 1e9, "debt_ratio": 20.0}
    tech = {"rsi14": 30.0, "return20d": -5.0, "sma60": 90.0}
    reasons, signal, score = apply_rules(tech, fund, 100.0)
    assert score <= 1.0


def test_calc_high_1y():
    """年內高點應合理"""
    df = _load_df()
    high_1y = _calc_high_1y(df)
    assert high_1y is not None
    assert high_1y > 0
    assert high_1y >= df["close"].iloc[-1] * 0.5  # 不會離當前價太離譜


def test_calc_monthly_revenue():
    """月營收應回傳 dict"""
    conn = _get_conn()
    result = calc_monthly_revenue("2330.TW", conn)
    conn.close()
    assert isinstance(result, dict)
    if result:
        assert "rev_consecutive_yoy" in result
        assert result["rev_consecutive_yoy"] > 0


def test_chip_reasons_format():
    """籌碼 reasons 應用「張」為單位，不含 K"""
    df = _load_df()
    tech = calc_indicators(df)
    conn = _get_conn()

    inst = conn.execute(
        "SELECT foreign_net, trust_net FROM institutional WHERE symbol=? ORDER BY date DESC LIMIT 60",
        ("2330.TW",)
    ).fetchall()
    if inst:
        tech["foreign_net_60d"] = sum(r["foreign_net"] or 0 for r in inst)
        tech["trust_net_60d"] = sum(r["trust_net"] or 0 for r in inst)
        tech["foreign_net_10d"] = sum(r["foreign_net"] or 0 for r in list(inst)[:10])
        tech["trust_net_10d"] = sum(r["trust_net"] or 0 for r in list(inst)[:10])

    fund = calc_fundamentals("2330.TW", conn, price=float(df["close"].iloc[-1]))
    monthly = calc_monthly_revenue("2330.TW", conn)
    conn.close()

    reasons, _, _ = apply_rules(tech, fund, float(df["close"].iloc[-1]), monthly)
    for r in reasons:
        if "外資" in r or "投信" in r:
            assert "K" not in r, f"reason 不應含 K 格式: {r}"
            assert "張" in r, f"reason 應含「張」: {r}"
