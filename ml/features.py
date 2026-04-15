"""
特徵工程
改進：
1. ROE 使用平均 equity（當季 + 前季平均），而非只用最新季
2. 負 EPS 時 PE 設為 NaN（虧損無意義）
3. 預測時籌碼特徵計算與訓練邏輯一致（60日滾動sum）
4. 修正股數推算：改用4季中位數減少配股/庫藏股雜訊
"""
import sqlite3
import pandas as pd
import numpy as np
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "stock.db"

FEATURE_COLS = [
    # 技術面
    "rsi14",
    "bb_pos",
    "sma20_bias", "sma60_bias",
    "vol_ratio",
    "return20d", "return60d",
    "atr_pct",
    # 動能 / 型態
    "momentum_12_1",       # 12-1 月動量
    "rs_pctile_60d",       # 60 日報酬在全市場的百分位
    "dist_from_52w_high",  # 距離 52 週高點 %（負值，越接近 0 越強）
    "new_high_20d",        # 突破 20 日新高旗標
    "consolidation_tight", # 20 日區間緊縮旗標（VCP 型態）
    # 突破 / 量價（新增）
    "breakout_with_volume",  # 突破 20 日新高 + 量比 > 1.5
    "vol_surge",             # 量比 > 2.0（爆量）
    "price_vol_bullish",     # 價漲量增（收盤 > 前日 + 量比 > 1.2）
    # 警告型（新增）
    "distribution_flag",     # 高檔出貨：近 5 日內創 20 日新高後，近 3 日量縮 + 跌
    "near_high_weak_rsi",    # 接近 52 週高點但 RSI > 75（背離風險）
    "vol_dry_down",          # 跌破 60 日低點 + 量縮（資金離場）
    # 基本面（已修正 lookahead bias）
    "eps_ttm", "roe", "debt_ratio", "revenue_yoy", "ni_yoy",
    "pe_ratio", "pb_ratio",
    # 月營收特徵
    "rev_consecutive_yoy", "rev_accel",
    # 籌碼面
    "margin_balance_chg",
    "short_balance_chg",
    # 短線籌碼（新增）
    "foreign_net_10d",       # 外資 10 日淨買（股）
    "trust_net_10d",         # 投信 10 日淨買（股）
    "both_inst_buying_10d",  # 外資+投信 10 日同步買超旗標
    "foreign_consec_buy",    # 外資連續買超天數（上限 10）
    "trust_consec_buy",      # 投信連續買超天數（上限 10）
]


def build_feature_matrix(conn=None, min_price_rows=120) -> pd.DataFrame:
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    symbols = [r[0] for r in conn.execute("SELECT symbol FROM stocks WHERE market='TSE'").fetchall()]

    all_financials = _load_all_financials(conn)
    all_inst = _load_all_institutional(conn)
    all_margin = _load_all_margin(conn)
    all_monthly = _load_all_monthly_revenue(conn)

    all_rows = []
    for symbol in symbols:
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume FROM stock_prices WHERE symbol=? ORDER BY date ASC",
            (symbol,)
        ).fetchall()
        if len(rows) < min_price_rows:
            continue

        df = pd.DataFrame([dict(r) for r in rows])
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")

        feats = _calc_price_features(df)
        if feats.empty:
            continue

        fund_ts = _get_fund_timeseries(symbol, all_financials, feats.index)
        for col in ["eps_ttm", "roe", "debt_ratio", "revenue_yoy", "ni_yoy"]:
            feats[col] = fund_ts.get(col, pd.Series(dtype=float))

        close_s = df["close"].reindex(feats.index)
        eps_ttm_s = fund_ts.get("eps_ttm")
        bvps_s = fund_ts.get("bvps")
        if eps_ttm_s is not None:
            # 負 EPS 時 PE 無意義，設為 NaN
            feats["pe_ratio"] = close_s / eps_ttm_s.replace(0, np.nan)
            feats["pe_ratio"] = feats["pe_ratio"].where(eps_ttm_s > 0)
        if bvps_s is not None:
            feats["pb_ratio"] = close_s / bvps_s.replace(0, np.nan)
            feats["pb_ratio"] = feats["pb_ratio"].where(feats["pb_ratio"] > 0)

        chip_feats = _get_chip_features(symbol, all_inst, all_margin, feats.index)
        for col in [
            "foreign_net_60d", "trust_net_60d",
            "foreign_net_10d", "trust_net_10d",
            "both_inst_buying_10d", "foreign_consec_buy", "trust_consec_buy",
            "margin_balance_chg", "short_balance_chg",
        ]:
            feats[col] = chip_feats.get(col, pd.Series(dtype=float))

        # 月營收特徵
        monthly_feats = _get_monthly_rev_timeseries(symbol, all_monthly, feats.index)
        for col in ["rev_consecutive_yoy", "rev_accel"]:
            if col in monthly_feats:
                feats[col] = monthly_feats[col]
            else:
                feats[col] = 0.0

        # 極端值 clip（避免影響 XGBoost split 品質）
        if "pe_ratio" in feats.columns:
            feats["pe_ratio"] = feats["pe_ratio"].clip(lower=0, upper=200)
        if "pb_ratio" in feats.columns:
            feats["pb_ratio"] = feats["pb_ratio"].clip(lower=0, upper=30)

        feats["symbol"] = symbol
        all_rows.append(feats)

    if close_conn:
        conn.close()

    if not all_rows:
        return pd.DataFrame()

    result = pd.concat(all_rows).reset_index().rename(columns={"index": "date"})

    # rs_pctile_60d：每個交易日，用 return60d 在全市場的百分位（0~100）
    result["rs_pctile_60d"] = result.groupby("date")["return60d"].rank(pct=True) * 100

    return result


def _calc_price_features(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]
    feats = pd.DataFrame(index=df.index)

    # RSI14（向量化 Wilder smoothing，比 pandas_ta 快很多）
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    feats["rsi14"] = 100 - 100 / (1 + rs)

    # Bollinger Band Position：(close - lower) / (upper - lower)
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    upper = sma20 + 2 * std20
    lower = sma20 - 2 * std20
    feats["bb_pos"] = (close - lower) / (upper - lower).replace(0, np.nan)

    sma60 = close.rolling(60).mean()
    feats["sma20_bias"] = (close - sma20) / sma20.replace(0, np.nan) * 100
    feats["sma60_bias"] = (close - sma60) / sma60.replace(0, np.nan) * 100

    vol20 = volume.rolling(20).mean()
    feats["vol_ratio"] = volume / vol20.replace(0, np.nan)

    feats["return20d"] = close.pct_change(20) * 100
    feats["return60d"] = close.pct_change(60) * 100

    # ATR%（向量化 Wilder smoothing）
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr14 = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    feats["atr_pct"] = atr14 / close.replace(0, np.nan) * 100

    # 12-1 月動量：跳過最近 22 個交易日避免短期反轉
    # (close[-22] / close[-252]) - 1
    feats["momentum_12_1"] = (close.shift(22) / close.shift(252) - 1) * 100

    # 距離 52 週（250 日）高點（負值，越接近 0 越強勢）
    high_52w = close.rolling(250, min_periods=60).max()
    feats["dist_from_52w_high"] = (close / high_52w - 1) * 100

    # 突破 20 日新高（用昨日以前的 20 日最高價比今天）
    high_20d_prev = close.rolling(20).max().shift(1)
    feats["new_high_20d"] = (close > high_20d_prev).astype(float)

    # 20 日區間緊縮：(20 日 high - 20 日 low) / close < 10% → VCP 型態
    high_20 = df["high"].rolling(20).max()
    low_20 = df["low"].rolling(20).min()
    range_pct = (high_20 - low_20) / close.replace(0, np.nan) * 100
    feats["consolidation_tight"] = (range_pct < 10).astype(float)

    # 突破 + 量增：突破 20 日新高 且 量比 > 1.5
    feats["breakout_with_volume"] = (
        feats["new_high_20d"].astype(bool) & (feats["vol_ratio"] > 1.5)
    ).astype(float)

    # 爆量：量比 > 2.0
    feats["vol_surge"] = (feats["vol_ratio"] > 2.0).astype(float)

    # 價漲量增（健康上漲）：收盤 > 前日 且 量比 > 1.2
    feats["price_vol_bullish"] = (
        (close > close.shift(1)) & (feats["vol_ratio"] > 1.2)
    ).astype(float)

    # 高檔出貨：近 5 日曾創 20 日新高，且近 3 日均量 < 前 20 日均量 且 收盤下跌
    had_new_high_5d = feats["new_high_20d"].rolling(5).max() > 0
    vol3 = volume.rolling(3).mean()
    vol20_prev = volume.shift(3).rolling(20).mean()
    ret_3d = close.pct_change(3)
    feats["distribution_flag"] = (
        had_new_high_5d & (vol3 < vol20_prev * 0.8) & (ret_3d < 0)
    ).astype(float)

    # 接近 52 週高點（< 5%）但 RSI > 75（動能背離）
    feats["near_high_weak_rsi"] = (
        (feats["dist_from_52w_high"] > -5) & (feats["rsi14"] > 75)
    ).astype(float)

    # 跌破 60 日低點 + 量縮（資金離場）
    low_60 = close.rolling(60).min()
    feats["vol_dry_down"] = (
        (close <= low_60 * 1.02) & (feats["vol_ratio"] < 0.7)
    ).astype(float)

    return feats


def _load_all_financials(conn) -> dict:
    rows = conn.execute(
        "SELECT symbol, year, quarter, revenue, net_income, eps, equity, total_assets, total_debt FROM financials ORDER BY symbol, year, quarter"
    ).fetchall()
    result = {}
    for r in rows:
        s = r[0]
        if s not in result:
            result[s] = []
        result[s].append(dict(r))
    return result


def _get_fund_timeseries(symbol: str, all_financials: dict, date_index: pd.DatetimeIndex) -> dict:
    """
    修正 lookahead bias：把每季財報轉成「公告日起生效」的時序，再 reindex 到 date_index。
    改進：
    - ROE 用平均 equity（當季 + 前季）
    - 負 EPS 時 PE 設 NaN
    - 股數用4季中位數，減少配股/庫藏股雜訊
    """
    records = all_financials.get(symbol, [])
    if not records:
        return {}

    def announce_date(year, quarter):
        # 保守估計公告日，避免 lookahead bias
        # Q4 法定期限 3/31 但多數公司到 4 月底才公告
        if quarter == 1:   return pd.Timestamp(year, 5, 15)
        elif quarter == 2: return pd.Timestamp(year, 8, 14)
        elif quarter == 3: return pd.Timestamp(year, 11, 14)
        else:              return pd.Timestamp(year + 1, 4, 30)

    df = pd.DataFrame(records)
    df["announce_date"] = df.apply(lambda r: announce_date(r["year"], r["quarter"]), axis=1)
    df = df.sort_values("announce_date").reset_index(drop=True)

    full_index = date_index.union(df["announce_date"])

    def _rolling_ttm(col):
        s_ann = df.set_index("announce_date")[col]
        ttm = s_ann.rolling(4, min_periods=2).sum()
        return ttm.reindex(full_index).ffill().bfill().reindex(date_index)

    def _latest(col):
        s_ann = df.set_index("announce_date")[col]
        return s_ann.reindex(full_index).ffill().bfill().reindex(date_index)

    eps_ttm = _rolling_ttm("eps")
    ni_ttm = _rolling_ttm("net_income")

    # ROE 改用平均 equity（當季 + 前季）
    equity_ann = df.set_index("announce_date")["equity"]
    avg_equity_ann = (equity_ann + equity_ann.shift(1)) / 2
    avg_equity_s = avg_equity_ann.reindex(full_index).ffill().bfill().reindex(date_index)
    roe = ni_ttm / avg_equity_s.replace(0, np.nan) * 100

    ta_s = _latest("total_assets")
    td_s = _latest("total_debt")
    debt_ratio = td_s / ta_s.replace(0, np.nan) * 100

    rev_yoy_ann = df.set_index("announce_date")["revenue"].pct_change(4) * 100
    ni_yoy_ann  = df.set_index("announce_date")["net_income"].pct_change(4) * 100
    ni_base = df.set_index("announce_date")["net_income"].shift(4)
    ni_yoy_ann = ni_yoy_ann.where((ni_base > 5e6) & (ni_yoy_ann < 500))
    rev_yoy = rev_yoy_ann.reindex(full_index).ffill().bfill().reindex(date_index)
    ni_yoy  = ni_yoy_ann.reindex(full_index).ffill().bfill().reindex(date_index)

    # 股數改用4季中位數，減少配股/庫藏股雜訊
    shares_candidates = []
    for _, r in df.iterrows():
        if r.get("eps") and r["eps"] != 0 and r.get("net_income"):
            s = r["net_income"] / r["eps"]
            if s > 0:
                shares_candidates.append(s)
    if shares_candidates:
        # 用最近4季的中位數
        shares_median = float(np.median(shares_candidates[-4:]))
    else:
        shares_median = None

    equity_s = _latest("equity")
    if shares_median and shares_median > 0:
        bvps_s = equity_s / shares_median
    else:
        bvps_s = None

    return {
        "eps_ttm": eps_ttm,
        "roe": roe,
        "debt_ratio": debt_ratio,
        "revenue_yoy": rev_yoy,
        "ni_yoy": ni_yoy,
        "bvps": bvps_s,
    }


def _load_all_institutional(conn) -> pd.DataFrame:
    rows = conn.execute(
        "SELECT symbol, date, foreign_net, trust_net, total_net FROM institutional ORDER BY symbol, date"
    ).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["symbol", "date", "foreign_net", "trust_net", "total_net"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def _load_all_margin(conn) -> pd.DataFrame:
    rows = conn.execute(
        "SELECT symbol, date, margin_balance, short_balance FROM margin_trading ORDER BY symbol, date"
    ).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["symbol", "date", "margin_balance", "short_balance"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def _get_chip_features(symbol: str, all_inst: pd.DataFrame, all_margin: pd.DataFrame, date_index: pd.DatetimeIndex) -> dict:
    result = {}

    if not all_inst.empty:
        inst = all_inst[all_inst["symbol"] == symbol].set_index("date")
        inst = inst.reindex(date_index)
        result["foreign_net_5d"]  = inst["foreign_net"].rolling(5).sum()
        result["trust_net_5d"]    = inst["trust_net"].rolling(5).sum()
        result["total_inst_5d"]   = inst["total_net"].rolling(5).sum()
        result["foreign_net_10d"] = inst["foreign_net"].rolling(10).sum()
        result["trust_net_10d"]   = inst["trust_net"].rolling(10).sum()
        result["foreign_net_60d"] = inst["foreign_net"].rolling(60).sum()
        result["trust_net_60d"]   = inst["trust_net"].rolling(60).sum()

        # 雙引擎：外資 10d 買 + 投信 10d 買
        CHIP_MIN = 500_000  # 股 = 500 張
        result["both_inst_buying_10d"] = (
            (result["foreign_net_10d"] > CHIP_MIN) & (result["trust_net_10d"] > CHIP_MIN)
        ).astype(float)

        # 連續買超天數（上限 10 避免極端值）
        fn = inst["foreign_net"].fillna(0)
        tn = inst["trust_net"].fillna(0)
        # rolling 10 日內連續為正的天數（從最近往前數）
        def _consec_buy(s: pd.Series) -> pd.Series:
            buy = (s > 0).astype(int)
            # 連買天數：(當日為買) × (前一日連買 + 1)
            grp = (buy == 0).cumsum()
            consec = buy.groupby(grp).cumsum()
            return consec.clip(upper=10)
        result["foreign_consec_buy"] = _consec_buy(fn)
        result["trust_consec_buy"]   = _consec_buy(tn)

    if not all_margin.empty:
        mg = all_margin[all_margin["symbol"] == symbol].set_index("date")
        mg = mg.reindex(date_index)
        mb = mg["margin_balance"]
        sb = mg["short_balance"]
        mb5 = mb.shift(5)
        sb5 = sb.shift(5)
        result["margin_balance_chg"] = (mb - mb5) / mb5.replace(0, np.nan) * 100
        result["short_balance_chg"]  = (sb - sb5) / sb5.replace(0, np.nan) * 100

    return result


def _get_fund_features(symbol: str, conn, price: float | None = None) -> dict:
    """用於 predict 的即時基本面（委託給 fundamentals.py 單一來源）"""
    from fundamentals import calc_fundamentals
    return calc_fundamentals(symbol, conn, price=price)


def _load_all_monthly_revenue(conn) -> dict:
    """載入所有月營收資料，回傳 {symbol: list of dicts}"""
    rows = conn.execute(
        "SELECT symbol, year, month, revenue, yoy, mom FROM monthly_revenue ORDER BY symbol, year, month"
    ).fetchall()
    if not rows:
        return {}
    result = {}
    for r in rows:
        s = r[0]
        if s not in result:
            result[s] = []
        result[s].append({"year": r[1], "month": r[2], "revenue": r[3], "yoy": r[4], "mom": r[5]})
    return result


def _get_monthly_rev_timeseries(symbol: str, all_monthly: dict, date_index: pd.DatetimeIndex) -> dict:
    """
    把月營收轉成時序特徵，對齊到 date_index。
    回傳 dict of pd.Series: rev_consecutive_yoy, rev_accel
    """
    records = all_monthly.get(symbol, [])
    if len(records) < 3:
        return {}

    df = pd.DataFrame(records)
    # 每月營收公告日約為次月10號
    df["date"] = df.apply(
        lambda r: pd.Timestamp(r["year"] + (1 if r["month"] == 12 else 0),
                               (r["month"] % 12) + 1, 10),
        axis=1
    )
    df = df.sort_values("date").set_index("date")
    df = df[~df.index.duplicated(keep="last")]

    full_index = date_index.union(df.index)
    full_index = full_index.drop_duplicates().sort_values()

    # 連續 YoY > 0 的月數
    yoy_positive = (df["yoy"].fillna(0) > 0).astype(int)
    breaks = (yoy_positive == 0).cumsum()
    consec = yoy_positive.groupby(breaks).cumsum()
    consec_s = consec.reindex(full_index).ffill().fillna(0).reindex(date_index)

    # 加速成長：本月 YoY > 上月 YoY > 0
    yoy_s = df["yoy"]
    yoy_prev = yoy_s.shift(1)
    accel = ((yoy_s > yoy_prev) & (yoy_prev > 0)).astype(int)
    accel_s = accel.reindex(full_index).ffill().fillna(0).reindex(date_index)

    return {
        "rev_consecutive_yoy": consec_s,
        "rev_accel": accel_s,
    }
