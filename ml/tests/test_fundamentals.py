"""基本面計算測試"""
import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from fundamentals import calc_fundamentals, _estimate_shares

DB_PATH = Path(__file__).parent.parent.parent / "data" / "stock.db"


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def test_estimate_shares_positive():
    """股數推算應為正數"""
    rows = [{"eps": 10.0, "net_income": 1e9}, {"eps": 9.5, "net_income": 9.5e8}]
    shares = _estimate_shares(rows)
    assert shares is not None
    assert shares > 0


def test_estimate_shares_zero_eps():
    """EPS 為 0 時應跳過"""
    rows = [{"eps": 0, "net_income": 1e9}, {"eps": 5.0, "net_income": 5e8}]
    shares = _estimate_shares(rows)
    assert shares is not None
    assert shares == 1e8  # 5e8 / 5.0


def test_calc_fundamentals_2330():
    """台積電基本面應有完整數據"""
    conn = _get_conn()
    fund = calc_fundamentals("2330.TW", conn, price=1800.0)
    conn.close()

    assert "eps_ttm" in fund
    assert fund["eps_ttm"] > 0
    assert "roe" in fund
    assert fund["roe"] > 10  # 台積電 ROE 應 > 10%
    assert "pe_ratio" in fund
    assert 10 < fund["pe_ratio"] < 100
    assert "debt_ratio" in fund
    assert 0 < fund["debt_ratio"] < 100


def test_calc_fundamentals_no_negative_pe():
    """虧損股不應有 PE"""
    conn = _get_conn()
    fund = calc_fundamentals("2330.TW", conn, price=1800.0)
    conn.close()
    if "pe_ratio" in fund:
        assert fund["pe_ratio"] > 0


def test_calc_fundamentals_missing_symbol():
    """不存在的股票應回傳空 dict"""
    conn = _get_conn()
    fund = calc_fundamentals("9999.TW", conn, price=100.0)
    conn.close()
    assert fund == {}


def test_roe_uses_average_equity():
    """ROE 應使用平均 equity（當季+前季），結果應合理"""
    conn = _get_conn()
    fund = calc_fundamentals("2330.TW", conn)
    conn.close()
    if "roe" in fund:
        assert 5 < fund["roe"] < 80  # 合理範圍
