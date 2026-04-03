"""
規則回測模組：計算每條規則的實際勝率，輸出 rule_scores.json
用法：python ml/backtest.py [--forward-days 10] [--min-samples 30]
"""
import sys
import json
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np
import pandas_ta as ta

sys.path.insert(0, str(Path(__file__).parent))
from features import _calc_price_features, _get_fund_timeseries, _load_all_financials

DB_PATH = Path(__file__).parent.parent / "data" / "stock.db"
RULE_SCORES_PATH = Path(__file__).parent / "rule_scores.json"

# 基本面規則去重窗口（天）：同一檔股票同一條件最多每60天計一次
FUND_DEDUP_DAYS = 60

# 各規則的原始 fallback 分數（若樣本不足時使用）
FALLBACK_SCORES = {
    "rsi_oversold":     0.80,
    "rsi_low":          0.60,
    "rsi_overbought":   0.20,
    "macd_golden_cross":0.85,
    "bb_lower":         0.65,
    "vol_surge":        0.60,
    "pullback":         0.55,
    "roe_high":         0.80,
    "roe_ok":           0.60,
    "revenue_yoy":      0.75,
    "ni_yoy":           0.80,
    "debt_low":         0.60,
}


def _load_price_panel(conn) -> dict:
    """載入所有股票的 OHLCV，回傳 {symbol: DataFrame}"""
    symbols = [r[0] for r in conn.execute("SELECT symbol FROM stocks").fetchall()]
    panel = {}
    for symbol in symbols:
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume FROM stock_prices WHERE symbol=? ORDER BY date ASC",
            (symbol,)
        ).fetchall()
        if len(rows) < 80:
            continue
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        panel[symbol] = df
    return panel


def _compute_market_returns(panel: dict, forward_days: int) -> pd.Series:
    """計算每個交易日的市場等權平均 N 日遠期報酬，作為 benchmark。
    過濾極端值（±50% 以上），避免除權息還原價或資料缺口污染 benchmark。
    """
    all_fwd = []
    for symbol, df in panel.items():
        close = df["close"]
        fwd = close.shift(-forward_days) / close - 1
        # 過濾異常：單筆超過 ±50% 視為資料錯誤
        fwd = fwd.where(fwd.abs() <= 0.5)
        fwd.name = symbol
        all_fwd.append(fwd)
    if not all_fwd:
        return pd.Series(dtype=float)
    combined = pd.concat(all_fwd, axis=1)
    return combined.mean(axis=1)


def _append_triggers(triggers: dict, rule: str, symbol: str, mask: pd.Series, fwd_ret: pd.Series):
    """將 mask=True 的日期批次加入 triggers，避免逐日迴圈"""
    idx = fwd_ret.index[mask & fwd_ret.notna() & np.isfinite(fwd_ret)]
    if len(idx) == 0:
        return
    rets = fwd_ret.loc[idx].values
    triggers[rule].extend(zip([symbol] * len(idx), idx, rets))


def _build_tech_triggers(panel: dict, all_financials: dict, forward_days: int) -> dict:
    """
    向量化版本：對每檔股票一次性計算所有規則的觸發布林矩陣，
    再批次 extend triggers，避免逐日 Python 迴圈。
    """
    triggers = {rule: [] for rule in FALLBACK_SCORES}

    total = len(panel)
    for i, (symbol, df) in enumerate(panel.items()):
        if (i + 1) % 100 == 0 or (i + 1) == total:
            print(f"  處理進度：{i+1}/{total}", flush=True)

        close = df["close"]

        feats = _calc_price_features(df)
        if feats.empty:
            continue

        fwd_ret = close.shift(-forward_days) / close - 1

        # ── 技術指標（向量化）──
        sma60 = ta.sma(close, length=60)  # 僅用於季線判斷，sma20 已移除（price_uptrend/sma_support 無效）

        bb = ta.bbands(close, length=20, std=2)
        bbl = None
        if bb is not None and not bb.empty:
            bbl_col = next((c for c in bb.columns if c.startswith("BBL_")), None)
            if bbl_col:
                bbl = bb[bbl_col]

        macd_df = ta.macd(close, fast=12, slow=26, signal=9)
        macd_hist = None
        if macd_df is not None and not macd_df.empty:
            hist_col = next((c for c in macd_df.columns if c.startswith("MACDh_")), None)
            if hist_col:
                macd_hist = macd_df[hist_col]

        rsi = feats.get("rsi14") if hasattr(feats, "get") else feats["rsi14"] if "rsi14" in feats.columns else None

        # RSI
        if rsi is not None:
            _append_triggers(triggers, "rsi_oversold", symbol, rsi < 30, fwd_ret)
            _append_triggers(triggers, "rsi_low",      symbol, (rsi >= 30) & (rsi < 40), fwd_ret)
            _append_triggers(triggers, "rsi_overbought", symbol, rsi > 70, fwd_ret)

        # MACD 黃金交叉（hist 由負轉正，僅保留翻轉瞬間）
        if macd_hist is not None:
            mh_prev = macd_hist.shift(1)
            _append_triggers(triggers, "macd_golden_cross", symbol,
                             (macd_hist > 0) & (mh_prev <= 0), fwd_ret)
            # macd_bullish、price_uptrend、sma_support 已移除（中長期回測無效）

        # 布林下軌
        if bbl is not None:
            _append_triggers(triggers, "bb_lower", symbol,
                             (bbl > 0) & (close < bbl * 1.02), fwd_ret)

        # 量增
        vol_ratio = feats["vol_ratio"] if "vol_ratio" in feats.columns else None
        if vol_ratio is not None:
            _append_triggers(triggers, "vol_surge", symbol, vol_ratio > 1.5, fwd_ret)

        # 近期回調（中長期視角：20日報酬 -10% ~ 0%，相對低點進場）
        r20 = feats["return20d"] if "return20d" in feats.columns else None
        if r20 is not None:
            _append_triggers(triggers, "pullback", symbol, (r20 > -10) & (r20 < 0), fwd_ret)

        # ── 基本面規則（向量化）──
        fund_ts = _get_fund_timeseries(symbol, all_financials, feats.index)

        roe_s = fund_ts.get("roe")
        if roe_s is not None:
            _append_triggers(triggers, "roe_high", symbol, roe_s >= 20, fwd_ret)
            _append_triggers(triggers, "roe_ok",   symbol, (roe_s >= 12) & (roe_s < 20), fwd_ret)

        rev_yoy = fund_ts.get("revenue_yoy")
        if rev_yoy is not None:
            _append_triggers(triggers, "revenue_yoy", symbol, rev_yoy > 5, fwd_ret)

        ni_yoy = fund_ts.get("ni_yoy")
        if ni_yoy is not None:
            _append_triggers(triggers, "ni_yoy", symbol, ni_yoy > 10, fwd_ret)

        debt_ratio = fund_ts.get("debt_ratio")
        if debt_ratio is not None:
            _append_triggers(triggers, "debt_low", symbol, debt_ratio < 50, fwd_ret)

    return triggers


def _dedup_fundamental_triggers(trigger_list: list) -> list:
    """
    基本面規則去重：同一檔股票每 FUND_DEDUP_DAYS 天最多保留一筆，
    避免連續觸發導致樣本自相關膨脹。
    """
    df = pd.DataFrame(trigger_list, columns=["symbol", "date", "fwd_ret"])
    df = df.sort_values(["symbol", "date"])
    result = []
    last_date_by_symbol = {}
    for _, row in df.iterrows():
        sym = row["symbol"]
        d = row["date"]
        last = last_date_by_symbol.get(sym)
        if last is None or (d - last).days >= FUND_DEDUP_DAYS:
            result.append((sym, d, row["fwd_ret"]))
            last_date_by_symbol[sym] = d
    return result


def _calc_win_rates(triggers: dict, market_returns: pd.Series, min_samples: int, forward_days: int,
                    market_abs_win_rate: float = 0.45) -> dict:
    """
    計算每條規則的勝率。
    - win_rate：絕對勝率（股票報酬 > 0），作為 score 基礎（反映「買進後賺錢的機率」）
    - excess_win_rate：相對勝率（超越大盤），作為診斷指標
    - avg_excess_return_pct：平均超額報酬（%），診斷用
    market_abs_win_rate：所有個股的 stock-level 正報酬率（基準線），
    suppression 以「rule win_rate < market_abs_win_rate」為準。
    """
    FUND_RULES = {"roe_high", "roe_ok", "revenue_yoy", "ni_yoy", "debt_low"}
    results = {}

    for rule, tlist in triggers.items():
        if rule in FUND_RULES:
            tlist = _dedup_fundamental_triggers(tlist)

        n = len(tlist)
        if n < min_samples:
            results[rule] = {
                "win_rate": None,
                "excess_win_rate": None,
                "avg_excess_return_pct": None,
                "sample_count": n,
                "score": FALLBACK_SCORES[rule],
                "status": "insufficient_data",
                "fallback_score": FALLBACK_SCORES[rule],
                "market_abs_win_rate": round(market_abs_win_rate, 4),
            }
            continue

        abs_rets = []      # 絕對報酬（用於勝率）
        excess_rets = []   # 超額報酬（診斷用）
        for symbol, date, stock_ret in tlist:
            if not np.isfinite(stock_ret):
                continue
            abs_rets.append(stock_ret)
            mkt = market_returns.loc[date] if date in market_returns.index else np.nan
            if np.isfinite(mkt):
                excess_rets.append(stock_ret - mkt)

        abs_arr = np.array(abs_rets, dtype=float)
        abs_arr = abs_arr[np.isfinite(abs_arr)]

        if len(abs_arr) < min_samples:
            results[rule] = {
                "win_rate": None,
                "excess_win_rate": None,
                "avg_excess_return_pct": None,
                "sample_count": n,
                "score": FALLBACK_SCORES[rule],
                "status": "insufficient_data",
                "fallback_score": FALLBACK_SCORES[rule],
                "market_abs_win_rate": round(market_abs_win_rate, 4),
            }
            continue

        # 絕對勝率：買進後 N 日報酬 > 0 的比例
        win_rate = float(np.mean(abs_arr > 0))

        # 超額報酬（僅有 market 數據時計算）
        exc_arr = np.array(excess_rets, dtype=float)
        exc_arr = exc_arr[np.isfinite(exc_arr)]
        excess_win_rate = float(np.mean(exc_arr > 0)) if len(exc_arr) >= min_samples else None
        avg_excess = float(np.mean(exc_arr) * 100) if len(exc_arr) >= min_samples else None

        # score = 絕對勝率（0~1），suppression 以「< 市場平均絕對勝率」為準
        score = win_rate

        results[rule] = {
            "win_rate": round(win_rate, 4),
            "excess_win_rate": round(excess_win_rate, 4) if excess_win_rate is not None else None,
            "avg_excess_return_pct": round(avg_excess, 4) if avg_excess is not None else None,
            "sample_count": n,
            "valid_sample_count": len(abs_arr),
            "score": round(score, 4),
            "status": "ok",
            "market_abs_win_rate": round(market_abs_win_rate, 4),
        }

    return results


def run_backtest(forward_days: int = 10, min_samples: int = 30):
    print(f"回測參數：forward_days={forward_days}, min_samples={min_samples}", flush=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("載入股價資料...", flush=True)
    panel = _load_price_panel(conn)
    print(f"  有效股票：{len(panel)} 檔", flush=True)

    if not panel:
        print("無價格資料，請先執行 sync.py", flush=True)
        conn.close()
        return

    print("計算市場基準報酬...", flush=True)
    market_returns = _compute_market_returns(panel, forward_days)

    print("載入財務資料...", flush=True)
    all_financials = _load_all_financials(conn)
    conn.close()

    print("枚舉規則觸發點（可能需要數分鐘）...", flush=True)
    triggers = _build_tech_triggers(panel, all_financials, forward_days)

    # 市場基準勝率（stock-level：所有個股 N 日正報酬率，作為 suppression 基準）
    print("計算市場個股基準勝率...", flush=True)
    all_raw_win = []
    for symbol, df in panel.items():
        close = df["close"]
        fwd = close.shift(-forward_days) / close - 1
        fwd = fwd.where(fwd.abs() <= 0.5).dropna()
        all_raw_win.extend((fwd.values > 0).tolist())
    mkt_win = float(np.mean(all_raw_win)) if all_raw_win else 0.45
    print(f"  市場個股基準勝率：{mkt_win:.2%}", flush=True)

    print("計算各規則勝率...", flush=True)
    rule_stats = _calc_win_rates(triggers, market_returns, min_samples, forward_days,
                                  market_abs_win_rate=mkt_win)

    # 輸出報告
    print("\n" + "="*75)
    print(f"市場平均絕對勝率（基準線）：{mkt_win:.2%}")
    print(f"{'規則':<22} {'樣本':>6} {'絕對勝率':>9} {'相對勝率':>9} {'超額%':>7} {'分數':>7} {'狀態'}")
    print("="*75)
    for rule, s in sorted(rule_stats.items()):
        if s["status"] == "ok":
            exc_wr = f"{s['excess_win_rate']:.2%}" if s["excess_win_rate"] is not None else "N/A"
            exc_pct = f"{s['avg_excess_return_pct']:>7.2f}" if s["avg_excess_return_pct"] is not None else "   N/A"
            marker = " +" if s["win_rate"] >= mkt_win else "  "
            print(f"{rule:<22} {s['sample_count']:>6} {s['win_rate']:>9.2%} {exc_wr:>9} {exc_pct} {s['score']:>7.4f}{marker}")
        else:
            print(f"{rule:<22} {s['sample_count']:>6} {'N/A':>9} {'N/A':>9} {'N/A':>7} {s['score']:>7.4f}  insufficient_data (fallback)")
    print("="*75)

    # 警告：絕對勝率低於市場基準的規則（會被 rule_engine 抑制）
    suppressed = [r for r, s in rule_stats.items() if s["status"] == "ok" and s["win_rate"] < mkt_win]
    if suppressed:
        print(f"\n[suppressed] 以下規則絕對勝率 < 市場基準 {mkt_win:.2%}，將被抑制（不計入分數）：")
        for r in suppressed:
            print(f"  {r}: win_rate={rule_stats[r]['win_rate']:.2%}")

    # 儲存結果
    output = {
        "generated_at": datetime.now().isoformat(),
        "forward_days": forward_days,
        "min_samples": min_samples,
        "market_abs_win_rate": round(mkt_win, 4),
        "rules": rule_stats,
    }
    with open(RULE_SCORES_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"\n結果已儲存：{RULE_SCORES_PATH}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--forward-days", type=int, default=60)
    parser.add_argument("--min-samples", type=int, default=30)
    args = parser.parse_args()
    run_backtest(forward_days=args.forward_days, min_samples=args.min_samples)
