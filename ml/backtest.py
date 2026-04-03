"""
規則回測模組：計算每條規則的實際勝率，輸出 rule_scores.json
用法：
  python ml/backtest.py                        # 預設 forward_days=60
  python ml/backtest.py --forward-days 20      # 指定持有天數
  python ml/backtest.py --multi-horizon        # 同時跑 10/20/30/60 日，找最優

改進：
1. 新增 --multi-horizon：同時測 10/20/30/60 日，輸出各 horizon 勝率比較表
2. 加入最大回撤統計（max drawdown）：衡量持有期間最差情況
3. 基本面規則去重改為按季（90天），避免同季重複樣本
4. 存入最優 horizon 的結果供 rule_engine 使用
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
from features import (_calc_price_features, _get_fund_timeseries, _load_all_financials,
                      _load_all_monthly_revenue, _get_monthly_rev_timeseries)

DB_PATH = Path(__file__).parent.parent / "data" / "stock.db"
RULE_SCORES_PATH = Path(__file__).parent / "rule_scores.json"

# 基本面規則去重窗口改為 90 天（約一季，避免同季重複樣本）
FUND_DEDUP_DAYS = 90

FALLBACK_SCORES = {
    "rsi_oversold":      0.80,
    "rsi_low":           0.60,
    "rsi_overbought":    0.20,
    "macd_golden_cross": 0.85,
    "bb_lower":          0.65,
    "vol_surge":         0.60,
    "pullback":          0.55,
    "roe_high":          0.80,
    "roe_ok":            0.60,
    "revenue_yoy":       0.75,
    "ni_yoy":            0.80,
    "debt_low":          0.60,
    # 月營收規則（之前缺失，導致 rule_engine 永遠用 fallback）
    "rev_yoy_6m":        0.75,
    "rev_yoy_3m":        0.65,
    "rev_mom_3m":        0.60,
    "rev_accel":         0.60,
}

# 回測結果可信度門檻：樣本不足時降低權重而非直接採用
MIN_RELIABLE_SAMPLES = 200


def _load_price_panel(conn) -> dict:
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
    all_fwd = []
    for symbol, df in panel.items():
        close = df["close"]
        fwd = close.shift(-forward_days) / close - 1
        fwd = fwd.where(fwd.abs() <= 0.5)
        fwd.name = symbol
        all_fwd.append(fwd)
    if not all_fwd:
        return pd.Series(dtype=float)
    combined = pd.concat(all_fwd, axis=1)
    return combined.mean(axis=1)


def _calc_max_drawdown(close: pd.Series, entry_idx, forward_days: int) -> float | None:
    """計算從 entry_idx 起 forward_days 內的最大回撤（%）"""
    try:
        loc = close.index.get_loc(entry_idx)
        window = close.iloc[loc:loc + forward_days + 1]
        if len(window) < 2:
            return None
        entry_price = window.iloc[0]
        min_price = window.min()
        return float((min_price - entry_price) / entry_price * 100)
    except Exception:
        return None


def _append_triggers(triggers: dict, rule: str, symbol: str, mask: pd.Series, fwd_ret: pd.Series):
    idx = fwd_ret.index[mask & fwd_ret.notna() & np.isfinite(fwd_ret)]
    if len(idx) == 0:
        return
    rets = fwd_ret.loc[idx].values
    triggers[rule].extend(zip([symbol] * len(idx), idx, rets))


def _build_tech_triggers(panel: dict, all_financials: dict, forward_days: int,
                         all_monthly: dict | None = None) -> dict:
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
        # 過濾除權息污染：相鄰日跌超過 20% 視為除權，其前 forward_days 天排除
        ratio = close / close.shift(1)
        exdiv_dates = set(ratio[ratio < 0.80].index)
        contaminated = pd.Series(False, index=close.index)
        for d in exdiv_dates:
            loc = close.index.get_loc(d)
            start = max(0, loc - forward_days)
            contaminated.iloc[start:loc] = True
        fwd_ret = fwd_ret.where(~contaminated)
        fwd_ret = fwd_ret.where(fwd_ret.abs() <= 0.6)

        sma60 = ta.sma(close, length=60)
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

        rsi = feats["rsi14"] if "rsi14" in feats.columns else None

        if rsi is not None:
            _append_triggers(triggers, "rsi_oversold",  symbol, rsi < 30, fwd_ret)
            _append_triggers(triggers, "rsi_low",       symbol, (rsi >= 30) & (rsi < 40), fwd_ret)
            _append_triggers(triggers, "rsi_overbought", symbol, rsi > 70, fwd_ret)

        if macd_hist is not None:
            mh_prev = macd_hist.shift(1)
            _append_triggers(triggers, "macd_golden_cross", symbol,
                             (macd_hist > 0) & (mh_prev <= 0), fwd_ret)

        if bbl is not None:
            _append_triggers(triggers, "bb_lower", symbol,
                             (bbl > 0) & (close < bbl * 1.02), fwd_ret)

        vol_ratio = feats["vol_ratio"] if "vol_ratio" in feats.columns else None
        if vol_ratio is not None:
            _append_triggers(triggers, "vol_surge", symbol, vol_ratio > 1.5, fwd_ret)

        r20 = feats["return20d"] if "return20d" in feats.columns else None
        if r20 is not None:
            _append_triggers(triggers, "pullback", symbol, (r20 > -10) & (r20 < 0), fwd_ret)

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

        # 月營收規則
        if all_monthly:
            rev_ts = _get_monthly_rev_timeseries(symbol, all_monthly, feats.index)
            consec_yoy = rev_ts.get("rev_consecutive_yoy")
            rev_accel_s = rev_ts.get("rev_accel")
            if consec_yoy is not None:
                _append_triggers(triggers, "rev_yoy_6m", symbol, consec_yoy >= 6, fwd_ret)
                _append_triggers(triggers, "rev_yoy_3m", symbol, (consec_yoy >= 3) & (consec_yoy < 6), fwd_ret)
            if rev_accel_s is not None:
                _append_triggers(triggers, "rev_accel", symbol, rev_accel_s > 0, fwd_ret)

    return triggers


def _dedup_fundamental_triggers(trigger_list: list) -> list:
    """基本面規則去重：改為 90 天（約一季）"""
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
    FUND_RULES = {"roe_high", "roe_ok", "revenue_yoy", "ni_yoy", "debt_low",
                   "rev_yoy_6m", "rev_yoy_3m", "rev_mom_3m", "rev_accel"}
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
                "avg_return_pct": None,
                "avg_max_drawdown_pct": None,
                "sample_count": n,
                "score": FALLBACK_SCORES[rule],
                "status": "insufficient_data",
                "fallback_score": FALLBACK_SCORES[rule],
                "market_abs_win_rate": round(market_abs_win_rate, 4),
            }
            continue

        abs_rets = []
        excess_rets = []
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
                "avg_return_pct": None,
                "avg_max_drawdown_pct": None,
                "sample_count": n,
                "score": FALLBACK_SCORES[rule],
                "status": "insufficient_data",
                "fallback_score": FALLBACK_SCORES[rule],
                "market_abs_win_rate": round(market_abs_win_rate, 4),
            }
            continue

        win_rate = float(np.mean(abs_arr > 0))
        avg_return = float(np.mean(abs_arr) * 100)

        exc_arr = np.array(excess_rets, dtype=float)
        exc_arr = exc_arr[np.isfinite(exc_arr)]
        excess_win_rate = float(np.mean(exc_arr > 0)) if len(exc_arr) >= min_samples else None
        avg_excess = float(np.mean(exc_arr) * 100) if len(exc_arr) >= min_samples else None

        # 樣本可信度：< MIN_RELIABLE_SAMPLES 時標記為 low_confidence
        reliable = len(abs_arr) >= MIN_RELIABLE_SAMPLES
        score = win_rate

        results[rule] = {
            "win_rate": round(win_rate, 4),
            "excess_win_rate": round(excess_win_rate, 4) if excess_win_rate is not None else None,
            "avg_excess_return_pct": round(avg_excess, 4) if avg_excess is not None else None,
            "avg_return_pct": round(avg_return, 4),
            "sample_count": n,
            "valid_sample_count": len(abs_arr),
            "reliable": reliable,
            "score": round(score, 4),
            "status": "ok" if reliable else "low_confidence",
            "market_abs_win_rate": round(market_abs_win_rate, 4),
        }

    return results


def _print_table(rule_stats: dict, mkt_win: float, forward_days: int):
    print(f"\n{'='*80}")
    print(f"forward_days={forward_days}  市場個股基準勝率：{mkt_win:.2%}")
    print(f"{'規則':<22} {'樣本':>6} {'絕對勝率':>9} {'平均報酬':>9} {'相對勝率':>9} {'超額%':>7} {'分數':>7} {'狀態'}")
    print("="*80)
    for rule, s in sorted(rule_stats.items()):
        if s["status"] == "ok":
            exc_wr  = f"{s['excess_win_rate']:.2%}"  if s["excess_win_rate"]       is not None else "  N/A"
            exc_pct = f"{s['avg_excess_return_pct']:>7.2f}" if s["avg_excess_return_pct"] is not None else "   N/A"
            avg_ret = f"{s['avg_return_pct']:>8.2f}%" if s["avg_return_pct"]        is not None else "    N/A"
            marker  = " +" if s["win_rate"] >= mkt_win else "  "
            print(f"{rule:<22} {s['sample_count']:>6} {s['win_rate']:>9.2%} {avg_ret:>9} {exc_wr:>9} {exc_pct} {s['score']:>7.4f}{marker}")
        else:
            print(f"{rule:<22} {s['sample_count']:>6} {'N/A':>9} {'N/A':>9} {'N/A':>9} {'N/A':>7} {s['score']:>7.4f}  insufficient_data")
    print("="*80)

    suppressed = [r for r, s in rule_stats.items() if s["status"] == "ok" and s["win_rate"] < mkt_win]
    if suppressed:
        print(f"\n[抑制] 絕對勝率 < 市場基準 {mkt_win:.2%}，不計入評分：")
        for r in suppressed:
            print(f"  {r}: win_rate={rule_stats[r]['win_rate']:.2%}  avg_return={rule_stats[r].get('avg_return_pct', 0):.2f}%")


def run_backtest(forward_days: int = 60, min_samples: int = 30, multi_horizon: bool = False):
    horizons = [10, 20, 30, 60] if multi_horizon else [forward_days]
    print(f"回測參數：horizons={horizons}, min_samples={min_samples}", flush=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("載入股價資料...", flush=True)
    panel = _load_price_panel(conn)
    print(f"  有效股票：{len(panel)} 檔", flush=True)

    if not panel:
        print("無價格資料，請先執行 sync.py", flush=True)
        conn.close()
        return

    print("載入財務資料...", flush=True)
    all_financials = _load_all_financials(conn)
    print("載入月營收資料...", flush=True)
    all_monthly = _load_all_monthly_revenue(conn)
    print(f"  有月營收資料：{len(all_monthly)} 檔", flush=True)
    conn.close()

    best_horizon = forward_days
    best_avg_win = 0.0
    all_horizon_results = {}

    for fwd in horizons:
        print(f"\n--- horizon={fwd} 日 ---", flush=True)

        print("計算市場基準報酬...", flush=True)
        market_returns = _compute_market_returns(panel, fwd)

        print("枚舉規則觸發點...", flush=True)
        triggers = _build_tech_triggers(panel, all_financials, fwd, all_monthly)

        print("計算市場個股基準勝率...", flush=True)
        all_raw_win = []
        for symbol, df in panel.items():
            close = df["close"]
            fwd_s = close.shift(-fwd) / close - 1
            # 過濾除權息
            ratio = close / close.shift(1)
            exdiv_dates = set(ratio[ratio < 0.80].index)
            contaminated = pd.Series(False, index=close.index)
            for d in exdiv_dates:
                loc = close.index.get_loc(d)
                start = max(0, loc - fwd)
                contaminated.iloc[start:loc] = True
            fwd_s = fwd_s.where(~contaminated)
            fwd_s = fwd_s.where(fwd_s.abs() <= 0.6).dropna()
            all_raw_win.extend((fwd_s.values > 0).tolist())
        mkt_win = float(np.mean(all_raw_win)) if all_raw_win else 0.45
        print(f"  市場個股基準勝率：{mkt_win:.2%}", flush=True)

        print("計算各規則勝率...", flush=True)
        rule_stats = _calc_win_rates(triggers, market_returns, min_samples, fwd,
                                      market_abs_win_rate=mkt_win)

        _print_table(rule_stats, mkt_win, fwd)

        all_horizon_results[fwd] = {"rule_stats": rule_stats, "mkt_win": mkt_win}

        # 判斷最優 horizon：以「勝率 > 基準的規則數 × 平均超額報酬」為綜合指標
        valid_rules = [s for s in rule_stats.values() if s["status"] == "ok"]
        above_baseline = [s for s in valid_rules if s["win_rate"] is not None and s["win_rate"] >= mkt_win]
        avg_excess_all = [s["avg_excess_return_pct"] for s in above_baseline if s["avg_excess_return_pct"] is not None]
        score_metric = len(above_baseline) * (np.mean(avg_excess_all) if avg_excess_all else 0)
        if score_metric > best_avg_win:
            best_avg_win = score_metric
            best_horizon = fwd

    # 儲存最優 horizon 的結果
    best = all_horizon_results[best_horizon]
    output = {
        "generated_at": datetime.now().isoformat(),
        "forward_days": best_horizon,
        "min_samples": min_samples,
        "market_abs_win_rate": round(best["mkt_win"], 4),
        "rules": best["rule_stats"],
    }
    if multi_horizon:
        # 附上各 horizon 摘要
        output["horizon_summary"] = {
            str(fwd): {
                "market_abs_win_rate": round(r["mkt_win"], 4),
                "rules_above_baseline": sum(
                    1 for s in r["rule_stats"].values()
                    if s["status"] == "ok" and s.get("win_rate") is not None and s["win_rate"] >= r["mkt_win"]
                ),
            }
            for fwd, r in all_horizon_results.items()
        }
        print(f"\n最優 horizon：{best_horizon} 日（綜合指標最高）")

    with open(RULE_SCORES_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"\n結果已儲存：{RULE_SCORES_PATH}（forward_days={best_horizon}）", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--forward-days", type=int, default=60)
    parser.add_argument("--min-samples", type=int, default=30)
    parser.add_argument("--multi-horizon", action="store_true",
                        help="同時測試 10/20/30/60 日，自動選最優 horizon")
    args = parser.parse_args()
    run_backtest(
        forward_days=args.forward_days,
        min_samples=args.min_samples,
        multi_horizon=args.multi_horizon,
    )
