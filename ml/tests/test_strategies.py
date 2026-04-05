"""策略模組測試"""
import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from strategies import calc_piotroski, calc_peg, calc_minervini

DB_PATH = Path(__file__).parent.parent.parent / "data" / "stock.db"


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def test_piotroski_score_range():
    """Piotroski 分數應在 0~6"""
    conn = _get_conn()
    result = calc_piotroski("2330.TW", conn)
    conn.close()
    if result:
        assert 0 <= result["piotroski"] <= 6


def test_piotroski_missing_symbol():
    """不存在的股票應回傳空 dict"""
    conn = _get_conn()
    result = calc_piotroski("9999.TW", conn)
    conn.close()
    assert result == {}


def test_peg_positive_growth():
    """EPS 成長 > 5% 時應有 PEG"""
    result = calc_peg({"pe_ratio": 20.0, "eps_ttm": 10.0, "eps_ttm_prev": 7.0})
    assert "peg" in result
    assert result["peg"] > 0


def test_peg_negative_growth():
    """EPS 衰退時不算 PEG"""
    result = calc_peg({"pe_ratio": 20.0, "eps_ttm": 5.0, "eps_ttm_prev": 10.0})
    assert result == {}


def test_minervini_score_range():
    """Minervini 分數應在 0~8"""
    tech = {"sma50": 100, "sma150": 95, "sma200": 90, "sma200_1m_ago": 88, "high_1y": 120, "low_1y": 70}
    result = calc_minervini(tech, 105.0)
    if result:
        assert 0 <= result["minervini"] <= 8


def test_minervini_missing_data():
    """缺少均線資料時應回傳空 dict"""
    result = calc_minervini({"sma50": 100}, 105.0)
    assert result == {}
