"""特徵工程測試"""
import sys
import sqlite3
from pathlib import Path

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
from features import _calc_price_features, FEATURE_COLS

DB_PATH = Path(__file__).parent.parent.parent / "data" / "stock.db"


def _load_price_df(symbol="2330.TW", limit=300):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume FROM stock_prices WHERE symbol=? ORDER BY date ASC LIMIT ?",
        (symbol, limit)
    ).fetchall()
    conn.close()
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    return df


def test_feature_cols_no_duplicates():
    """FEATURE_COLS 不應有重複"""
    assert len(FEATURE_COLS) == len(set(FEATURE_COLS))


def test_feature_cols_no_foreign_trust():
    """foreign_net_60d / trust_net_60d 不應在 ML 特徵中（覆蓋率不足）"""
    assert "foreign_net_60d" not in FEATURE_COLS
    assert "trust_net_60d" not in FEATURE_COLS


def test_calc_price_features_shape():
    """價格特徵應有正確的欄位"""
    df = _load_price_df()
    feats = _calc_price_features(df)
    assert not feats.empty
    for col in ["rsi14", "bb_pos", "sma20_bias", "sma60_bias", "vol_ratio", "return20d", "return60d", "atr_pct"]:
        assert col in feats.columns, f"缺少 {col}"


def test_calc_price_features_no_inf():
    """價格特徵不應有 inf"""
    df = _load_price_df()
    feats = _calc_price_features(df)
    numeric = feats.select_dtypes(include=[np.number])
    assert not np.isinf(numeric.values[~np.isnan(numeric.values)]).any()


def test_rsi_range():
    """RSI 應在 0~100"""
    df = _load_price_df()
    feats = _calc_price_features(df)
    rsi = feats["rsi14"].dropna()
    assert (rsi >= 0).all()
    assert (rsi <= 100).all()


def test_vol_ratio_positive():
    """量比應為正數"""
    df = _load_price_df()
    feats = _calc_price_features(df)
    vr = feats["vol_ratio"].dropna()
    assert (vr > 0).all()
