"""
特徵工程
- 修正 lookahead bias：每個訓練點只用「該日期之前」已知的財務資料
- 新增籌碼特徵：三大法人淨買、融資餘額變化
"""
import sqlite3
import pandas as pd
import numpy as np
import pandas_ta as ta
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "stock.db"

FEATURE_COLS = [
    # 技術面（中長期視角，保留趨勢和位置，短線指標降權）
    "rsi14",
    "bb_pos",
    "sma20_bias", "sma60_bias",
    "vol_ratio",
    "return20d", "return60d",
    "atr_pct",
    # 基本面（已修正 lookahead bias）—— 中長期核心訊號
    "eps_ttm", "roe", "debt_ratio", "revenue_yoy", "ni_yoy",
    "pe_ratio", "pb_ratio",
    # 籌碼面（法人動向對中長期有指向性）
    "foreign_net_60d",
    "trust_net_60d",
    "margin_balance_chg",
    "short_balance_chg",
]


def build_feature_matrix(conn=None, min_price_rows=120) -> pd.DataFrame:
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True

    symbols = [r[0] for r in conn.execute("SELECT symbol FROM stocks").fetchall()]

    # 預載所有財務資料（修正 lookahead bias 用）
    all_financials = _load_all_financials(conn)
    # 預載籌碼資料
    all_inst = _load_all_institutional(conn)
    all_margin = _load_all_margin(conn)

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

        # 財務特徵（修正 lookahead bias）
        fund_ts = _get_fund_timeseries(symbol, all_financials, feats.index)
        for col in ["eps_ttm", "roe", "debt_ratio", "revenue_yoy", "ni_yoy"]:
            feats[col] = fund_ts.get(col, pd.Series(dtype=float))

        # PE / PB（股價已在 feats.index 上，搭配 eps_ttm / bvps 計算）
        close_s = df["close"].reindex(feats.index)
        eps_ttm_s = fund_ts.get("eps_ttm")
        bvps_s = fund_ts.get("bvps")
        if eps_ttm_s is not None:
            feats["pe_ratio"] = close_s / eps_ttm_s.replace(0, np.nan)
            feats["pe_ratio"] = feats["pe_ratio"].where(feats["pe_ratio"] > 0)  # 虧損時 PE 無意義
        if bvps_s is not None:
            feats["pb_ratio"] = close_s / bvps_s.replace(0, np.nan)
            feats["pb_ratio"] = feats["pb_ratio"].where(feats["pb_ratio"] > 0)

        # 籌碼特徵
        chip_feats = _get_chip_features(symbol, all_inst, all_margin, feats.index)
        for col in ["foreign_net_60d", "trust_net_60d", "margin_balance_chg", "short_balance_chg"]:
            feats[col] = chip_feats.get(col, pd.Series(dtype=float))

        feats["symbol"] = symbol
        all_rows.append(feats)

    if close_conn:
        conn.close()

    if not all_rows:
        return pd.DataFrame()

    result = pd.concat(all_rows).reset_index().rename(columns={"index": "date"})
    return result


def _calc_price_features(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"]
    volume = df["volume"]
    feats = pd.DataFrame(index=df.index)

    rsi14 = ta.rsi(close, length=14)
    rsi6 = ta.rsi(close, length=6)
    feats["rsi14"] = rsi14
    feats["rsi6"] = rsi6

    macd = ta.macd(close, fast=12, slow=26, signal=9)
    if macd is not None and not macd.empty:
        feats["macd"] = macd.get("MACD_12_26_9")
        feats["macd_signal"] = macd.get("MACDs_12_26_9")
        feats["macd_hist"] = macd.get("MACDh_12_26_9")

    bb = ta.bbands(close, length=20, std=2)
    if bb is not None and not bb.empty:
        bbp_col = next((c for c in bb.columns if c.startswith("BBP_")), None)
        if bbp_col:
            feats["bb_pos"] = bb[bbp_col]

    sma20 = ta.sma(close, length=20)
    sma60 = ta.sma(close, length=60)
    feats["sma20_bias"] = (close - sma20) / sma20.replace(0, np.nan) * 100
    feats["sma60_bias"] = (close - sma60) / sma60.replace(0, np.nan) * 100

    vol20 = volume.rolling(20).mean()
    feats["vol_ratio"] = volume / vol20.replace(0, np.nan)

    feats["return20d"] = close.pct_change(20) * 100
    feats["return60d"] = close.pct_change(60) * 100

    atr = ta.atr(df["high"], df["low"], close, length=14)
    if atr is not None:
        feats["atr_pct"] = atr / close.replace(0, np.nan) * 100

    return feats


def _load_all_financials(conn) -> dict:
    """載入所有財務資料，key=symbol，value=按季度排序的 list"""
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
    修正 lookahead bias（向量化版本）：
    把每季財報轉成「公告日起生效」的時序，再 reindex 到 date_index。
    公告時程：Q1→5/15, Q2→8/14, Q3→11/14, Q4→隔年3/31
    """
    records = all_financials.get(symbol, [])
    if not records:
        return {}

    def announce_date(year, quarter):
        if quarter == 1:   return pd.Timestamp(year, 5, 15)
        elif quarter == 2: return pd.Timestamp(year, 8, 14)
        elif quarter == 3: return pd.Timestamp(year, 11, 14)
        else:              return pd.Timestamp(year + 1, 3, 31)

    df = pd.DataFrame(records)
    df["announce_date"] = df.apply(lambda r: announce_date(r["year"], r["quarter"]), axis=1)
    df = df.sort_values("announce_date").reset_index(drop=True)

    # 建立「公告日」為 index 的時序，用 ffill 填充到交易日
    full_index = date_index.union(df["announce_date"])

    def _rolling_ttm(col):
        # 先在公告日 index 做 rolling sum（只有 N 個點，不受 NaN 污染）
        # 再 ffill/bfill 到完整 date_index
        s_ann = df.set_index("announce_date")[col]
        ttm = s_ann.rolling(4, min_periods=1).sum()
        return ttm.reindex(full_index).ffill().bfill().reindex(date_index)

    def _latest(col):
        s_ann = df.set_index("announce_date")[col]
        return s_ann.reindex(full_index).ffill().bfill().reindex(date_index)

    eps_ttm = _rolling_ttm("eps")
    ni_ttm = _rolling_ttm("net_income")
    equity_s = _latest("equity")
    roe = ni_ttm / equity_s.replace(0, np.nan) * 100

    ta_s = _latest("total_assets")
    td_s = _latest("total_debt")
    debt_ratio = td_s / ta_s.replace(0, np.nan) * 100

    # YoY：先在公告日序列做 pct_change(4)（4季前），再 ffill/bfill 到交易日
    rev_yoy_ann = df.set_index("announce_date")["revenue"].pct_change(4) * 100
    ni_yoy_ann  = df.set_index("announce_date")["net_income"].pct_change(4) * 100
    # 基期為負（虧損轉盈）或超過 500% 視為失真，設為 NaN
    ni_base = df.set_index("announce_date")["net_income"].shift(4)
    # 基期需 > 500萬（避免近零基期造成虛高 YoY），且 YoY < 500%
    ni_yoy_ann = ni_yoy_ann.where((ni_base > 5e6) & (ni_yoy_ann < 500))
    rev_yoy = rev_yoy_ann.reindex(full_index).ffill().bfill().reindex(date_index)
    ni_yoy  = ni_yoy_ann.reindex(full_index).ffill().bfill().reindex(date_index)

    # 每股淨值（bvps）：用單季 net_income / eps 推算股數，避免需要另存股本資料
    ni_q_s  = _latest("net_income")
    eps_q_s = _latest("eps")
    shares_s = ni_q_s / eps_q_s.replace(0, np.nan)
    bvps_s = equity_s / shares_s.replace(0, np.nan)

    return {
        "eps_ttm": eps_ttm,
        "roe": roe,
        "debt_ratio": debt_ratio,
        "revenue_yoy": rev_yoy,
        "ni_yoy": ni_yoy,
        "bvps": bvps_s,   # 每股淨值，給 build_feature_matrix 計算 PB
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
        result["foreign_net_5d"] = inst["foreign_net"].rolling(5).sum()
        result["trust_net_5d"] = inst["trust_net"].rolling(5).sum()
        result["total_inst_5d"] = inst["total_net"].rolling(5).sum()
        # 中長期籌碼：60 日累計（約 1 季，確認法人真正在建倉）
        result["foreign_net_60d"] = inst["foreign_net"].rolling(60).sum()
        result["trust_net_60d"] = inst["trust_net"].rolling(60).sum()

    if not all_margin.empty:
        mg = all_margin[all_margin["symbol"] == symbol].set_index("date")
        mg = mg.reindex(date_index)
        mb = mg["margin_balance"]
        sb = mg["short_balance"]
        mb5 = mb.shift(5)
        sb5 = sb.shift(5)
        result["margin_balance_chg"] = (mb - mb5) / mb5.replace(0, np.nan) * 100
        result["short_balance_chg"] = (sb - sb5) / sb5.replace(0, np.nan) * 100

    return result


def _get_fund_features(symbol: str, conn, price: float | None = None) -> dict:
    """用於 rule_engine / predict 的即時基本面（非訓練用，取最新已有資料）"""
    rows = conn.execute(
        "SELECT revenue, net_income, eps, equity, total_assets, total_debt FROM financials WHERE symbol=? ORDER BY year DESC, quarter DESC LIMIT 8",
        (symbol,)
    ).fetchall()
    result = {}
    if not rows:
        return result
    rows = [dict(r) for r in rows]
    ni_list = [r["net_income"] for r in rows[:4] if r["net_income"] is not None]
    equity = rows[0].get("equity")
    ni_q = rows[0].get("net_income")
    eps_q = rows[0].get("eps")
    # 推算股數（用於補算 eps=None 的季度）
    shares = None
    for r in rows[:4]:
        if r.get("eps") and r["eps"] != 0 and r.get("net_income"):
            shares = r["net_income"] / r["eps"]
            if shares > 0:
                break
    # EPS TTM：eps=None 但有 net_income 時用股數反推
    eps_parts = []
    for r in rows[:4]:
        if r.get("eps") is not None:
            eps_parts.append(r["eps"])
        elif r.get("net_income") is not None and shares and shares > 0:
            eps_parts.append(r["net_income"] / shares)
    if eps_parts:
        result["eps_ttm"] = sum(eps_parts)
    if ni_list and equity and equity > 0:
        result["roe"] = sum(ni_list) / equity * 100
    if rows[0].get("total_assets") and rows[0]["total_assets"] > 0 and rows[0].get("total_debt") is not None:
        result["debt_ratio"] = rows[0]["total_debt"] / rows[0]["total_assets"] * 100
    if len(rows) >= 5 and rows[0].get("revenue") and rows[4].get("revenue") and rows[4]["revenue"] > 0:
        result["revenue_yoy"] = (rows[0]["revenue"] - rows[4]["revenue"]) / rows[4]["revenue"] * 100
    if len(rows) >= 5 and rows[0].get("net_income") and rows[4].get("net_income"):
        base_ni = rows[0]["net_income"]
        prev_ni = rows[4]["net_income"]
        if prev_ni > 0 and prev_ni > abs(base_ni) * 0.05:
            yoy = (base_ni - prev_ni) / prev_ni * 100
            if yoy <= 500:
                result["ni_yoy"] = yoy
    # PE / PB（需要股價）
    if price and price > 0:
        eps_ttm = result.get("eps_ttm")
        if eps_ttm and eps_ttm > 0:
            result["pe_ratio"] = price / eps_ttm
        if ni_q and eps_q and eps_q != 0 and equity and equity > 0:
            shares = ni_q / eps_q
            if shares > 0:
                bvps = equity / shares
                result["pb_ratio"] = price / bvps
    return result
