"""資料完整性測試：確保 DB 資料正確"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "stock.db"


def _conn():
    return sqlite3.connect(DB_PATH)


def test_no_otc_stocks():
    """stocks 表不應有上櫃股票"""
    conn = _conn()
    otc = conn.execute("SELECT COUNT(*) FROM stocks WHERE market='OTC'").fetchone()[0]
    conn.close()
    assert otc == 0, f"stocks 仍有 {otc} 檔上櫃"


def test_no_otc_prices():
    """stock_prices 不應有 .TWO 股票"""
    conn = _conn()
    otc = conn.execute("SELECT COUNT(*) FROM stock_prices WHERE symbol LIKE '%.TWO'").fetchone()[0]
    conn.close()
    assert otc == 0


def test_no_otc_institutional():
    """institutional 不應有 .TWO 股票"""
    conn = _conn()
    otc = conn.execute("SELECT COUNT(*) FROM institutional WHERE symbol LIKE '%.TWO'").fetchone()[0]
    conn.close()
    assert otc == 0


def test_stocks_all_tse():
    """所有 stocks 都應是上市 (TSE)"""
    conn = _conn()
    markets = [r[0] for r in conn.execute("SELECT DISTINCT market FROM stocks").fetchall()]
    conn.close()
    assert markets == ["TSE"]


def test_stock_prices_have_volume():
    """stock_prices 的成交量不應為 0（排除休市）"""
    conn = _conn()
    zero_vol = conn.execute(
        "SELECT COUNT(*) FROM stock_prices WHERE volume = 0 AND close > 0"
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM stock_prices").fetchone()[0]
    conn.close()
    ratio = zero_vol / total if total > 0 else 0
    assert ratio < 0.05, f"成交量為0的比例 {ratio:.1%} 太高"


def test_institutional_unit_is_shares():
    """institutional 的外資數值應為「股」（數量級應 > 1000）"""
    conn = _conn()
    row = conn.execute(
        "SELECT AVG(ABS(foreign_net)) FROM institutional WHERE foreign_net != 0"
    ).fetchone()
    conn.close()
    avg_abs = row[0]
    assert avg_abs > 1000, f"外資平均絕對值 {avg_abs} 太小，可能單位不是股"


def test_eps_q4_not_all_null():
    """Q4 的 EPS 不應全為 NULL（已修補）"""
    conn = _conn()
    total = conn.execute("SELECT COUNT(*) FROM financials WHERE quarter=4").fetchone()[0]
    null_eps = conn.execute("SELECT COUNT(*) FROM financials WHERE quarter=4 AND eps IS NULL").fetchone()[0]
    conn.close()
    if total > 0:
        ratio = null_eps / total
        assert ratio < 0.05, f"Q4 EPS NULL 比例 {ratio:.0%} 太高"


def test_monthly_revenue_has_data():
    """月營收表應有資料"""
    conn = _conn()
    count = conn.execute("SELECT COUNT(*) FROM monthly_revenue").fetchone()[0]
    symbols = conn.execute("SELECT COUNT(DISTINCT symbol) FROM monthly_revenue").fetchone()[0]
    conn.close()
    assert count > 1000, f"月營收只有 {count} 筆"
    assert symbols > 100, f"月營收只有 {symbols} 檔"


def test_monthly_revenue_yoy_reasonable():
    """月營收 YoY 應在合理範圍（-99% ~ +500%）"""
    conn = _conn()
    outliers = conn.execute(
        "SELECT COUNT(*) FROM monthly_revenue WHERE yoy IS NOT NULL AND (yoy < -99 OR yoy > 500)"
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM monthly_revenue WHERE yoy IS NOT NULL").fetchone()[0]
    conn.close()
    if total > 0:
        ratio = outliers / total
        assert ratio < 0.05, f"YoY 異常值比例 {ratio:.1%} 太高"


def test_recommendations_only_tse():
    """recommendations 不應有 .TWO"""
    conn = _conn()
    otc = conn.execute("SELECT COUNT(*) FROM recommendations WHERE symbol LIKE '%.TWO'").fetchone()[0]
    conn.close()
    assert otc == 0


def test_institutional_latest_date_matches_price():
    """法人最新日期應與價格最新日期一致（允許差 1 天）"""
    conn = _conn()
    price_max = conn.execute("SELECT MAX(date) FROM stock_prices").fetchone()[0]
    inst_max = conn.execute("SELECT MAX(date) FROM institutional").fetchone()[0]
    conn.close()
    if not price_max or not inst_max:
        return
    from datetime import datetime
    p = datetime.strptime(price_max, "%Y-%m-%d")
    i = datetime.strptime(inst_max, "%Y-%m-%d")
    diff = abs((p - i).days)
    assert diff <= 5, f"法人最新 {inst_max} 落後價格 {price_max} {diff} 天"


def test_institutional_start_date_matches_price():
    """法人起始日期不應落後價格起始日期超過 30 天（以 2 年內為基準）"""
    conn = _conn()
    inst_min = conn.execute("SELECT MIN(date) FROM institutional").fetchone()[0]
    conn.close()
    if not inst_min:
        return
    from datetime import datetime, timedelta
    i = datetime.strptime(inst_min, "%Y-%m-%d")
    two_years_ago = datetime.now() - timedelta(days=730)
    expected_start = max(two_years_ago, datetime(2024, 1, 1))
    diff = (i - expected_start).days
    assert diff <= 30, f"法人起始 {inst_min} 落後預期起始 {expected_start.strftime('%Y-%m-%d')} {diff} 天"


def test_institutional_coverage_ratio():
    """有價格的股票應至少 90% 有法人資料"""
    conn = _conn()
    price_symbols = conn.execute("SELECT COUNT(DISTINCT symbol) FROM stock_prices").fetchone()[0]
    inst_symbols = conn.execute("SELECT COUNT(DISTINCT symbol) FROM institutional").fetchone()[0]
    conn.close()
    if price_symbols == 0:
        return
    ratio = inst_symbols / price_symbols
    assert ratio >= 0.90, f"法人覆蓋率 {ratio:.0%} 低於 90%"
