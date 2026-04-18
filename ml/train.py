"""
XGBoost 模型訓練
用法：python ml/train.py
輸出：ml/model.pkl

改進：
1. 混合標籤：相對強勢前30% + 絕對報酬>0（避免熊市學「虧少」）
2. 過濾除權息污染：相鄰日收盤跌超過20%視為除權，排除其後60日的label
3. 樣本不平衡處理：設定 scale_pos_weight
4. 儲存 mean_auc 供 predict.py 動態調整 ML 權重
"""
import sys
import sqlite3
import pickle
from pathlib import Path

import pandas as pd
import numpy as np
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import roc_auc_score
import xgboost as xgb

sys.path.insert(0, str(Path(__file__).parent))
from features import build_feature_matrix, FEATURE_COLS

DB_PATH = Path(__file__).parent.parent / "data" / "stock.db"
MODEL_PATH = Path(__file__).parent / "model.pkl"
FORWARD_DAYS = 20   # 短期：20 交易日（約 1 個月）
RELATIVE_TOP_PCT = 0.20   # 前20%相對強勢視為正例
MIN_ABSOLUTE_RET = 0.05   # 且絕對報酬需 > 5%（過濾橫盤、只留真噴發）

# ───── 多模型特徵子集（specialized ensemble）─────
# 每個模型專注一種 alpha pattern，避免特徵互相稀釋

BREAKOUT_FEATURES = [
    # 突破/型態/動能
    "momentum_12_1", "rs_pctile_60d", "dist_from_52w_high",
    "return20d", "return60d",
    "new_high_20d", "consolidation_tight", "breakout_with_volume",
    "vol_surge", "price_vol_bullish", "vol_ratio",
    "distribution_flag", "near_high_weak_rsi",
    "sma20_bias", "sma60_bias", "atr_pct", "bb_pos", "rsi14",
    "rs_vs_industry_20d", "industry_momentum",
    "rel_strength_vs_mkt", "market_return_20d",
    "foreign_fut_net_oi", "foreign_fut_oi_chg_5d",  # 大盤方向
]

VALUE_FEATURES = [
    # 估值 + 基本面
    "pe_ratio", "pb_ratio", "peg_ratio",           # 加 PEG 校正成長率
    "eps_ttm", "pe_pct_in_industry",
    "roe", "debt_ratio", "revenue_yoy", "ni_yoy",
    "rev_consecutive_yoy", "rev_accel",
    "market_return_60d", "beta_60d",
    "earnings_drift",  # 公告後動能
]

CHIP_FEATURES = [
    # 中期籌碼（主力建倉訊號，權重加重）
    "foreign_net_60d", "trust_net_60d",
    # 短期籌碼
    "foreign_net_10d", "trust_net_10d", "both_inst_buying_10d",
    "foreign_consec_buy", "trust_consec_buy",
    "margin_balance_chg", "short_balance_chg",
    # 事件窗口
    "ex_div_window", "post_ex_div_recovery", "near_earnings",
    # 輔助（移除 return20d 避免模型學成「最近漲很多」）
    "return60d",              # 改用中期報酬，過濾短線噴出
    "rs_vs_industry_20d",     # 相對產業強度
    "vol_ratio",
    # 大盤背景
    "foreign_fut_net_oi", "foreign_fut_oi_chg_5d",
]


def _mark_exdiv_windows(price_df: pd.DataFrame, forward_days: int) -> pd.Series:
    """
    標記除權息污染窗口。
    相鄰日收盤跌超過20% → 視為除權息，其前 forward_days 天的 label 會被污染。
    回傳 bool Series（True=污染，應排除）。
    """
    close = price_df["close"]
    ratio = close / close.shift(1)
    exdiv_dates = ratio[ratio < 0.80].index
    contaminated = pd.Series(False, index=price_df.index)
    for d in exdiv_dates:
        loc = price_df.index.get_loc(d)
        start = max(0, loc - forward_days)
        contaminated.iloc[start:loc] = True
    return contaminated


def train():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("建立特徵矩陣...", flush=True)
    feat_df = build_feature_matrix(conn)

    if feat_df.empty:
        print("特徵矩陣為空，請確認資料已同步", flush=True)
        conn.close()
        return

    feat_df["date"] = pd.to_datetime(feat_df["date"])
    print(f"特徵矩陣：{len(feat_df)} 筆，{feat_df['symbol'].nunique()} 檔", flush=True)

    print("計算標籤（相對強勢）...", flush=True)
    labeled_parts = []

    for symbol, g in feat_df.groupby("symbol"):
        g = g.sort_values("date").copy()
        price_rows = conn.execute(
            "SELECT date, close FROM stock_prices WHERE symbol=? ORDER BY date ASC",
            (symbol,)
        ).fetchall()
        if not price_rows:
            continue
        price_df = pd.DataFrame(price_rows, columns=["date", "close"])
        price_df["date"] = pd.to_datetime(price_df["date"])
        price_df = price_df.set_index("date")

        g2 = g.set_index("date").copy()
        g2 = g2.join(price_df[["close"]], how="left")
        g2["forward_ret"] = g2["close"].shift(-FORWARD_DAYS) / g2["close"] - 1
        g2["symbol"] = symbol

        # 標記除權息污染窗口
        contaminated = _mark_exdiv_windows(price_df, FORWARD_DAYS)
        g2 = g2.join(contaminated.rename("contaminated"), how="left")
        g2["contaminated"] = g2["contaminated"].fillna(False)

        labeled_parts.append(g2.reset_index())

    conn.close()

    if not labeled_parts:
        print("無有效標籤資料", flush=True)
        return

    combined = pd.concat(labeled_parts, ignore_index=True)

    # 排除除權息污染與極端值（±60%以上視為資料缺口）
    combined = combined[~combined["contaminated"]]
    combined = combined[combined["forward_ret"].abs() <= 0.60]
    combined = combined.dropna(subset=["forward_ret"])

    # 去最後 FORWARD_DAYS 個交易日（避免 label 洩漏）
    all_dates = sorted(combined["date"].unique())
    if len(all_dates) > FORWARD_DAYS:
        cutoff = all_dates[-FORWARD_DAYS]
        combined = combined[combined["date"] < cutoff]

    # ── 混合標籤：20日 相對前20% 且 絕對報酬 > 5% ──
    # 嚴格標籤讓模型專注學「真正會噴」的訊號，提高 AUC
    print(f"計算混合標籤（{FORWARD_DAYS}日 top {RELATIVE_TOP_PCT:.0%} + 絕對報酬>{MIN_ABSOLUTE_RET:.0%}）...", flush=True)
    threshold_per_day = combined.groupby("date")["forward_ret"].transform(
        lambda x: x.quantile(1 - RELATIVE_TOP_PCT)
    )
    combined["label"] = (
        (combined["forward_ret"] >= threshold_per_day) &
        (combined["forward_ret"] > MIN_ABSOLUTE_RET)
    ).astype(int)
    combined = combined.dropna(subset=["label"])

    X = combined[FEATURE_COLS].astype(float)
    X = X.replace([np.inf, -np.inf], np.nan)
    # 籌碼特徵 NaN 填 0（= 中性，因為籌碼資料覆蓋期短於價格資料，0 有語意）
    CHIP_ZERO_COLS = [
        "foreign_net_60d", "trust_net_60d",
        "foreign_net_10d", "trust_net_10d",
        "both_inst_buying_10d",
        "foreign_consec_buy", "trust_consec_buy",
        "margin_balance_chg", "short_balance_chg",
        "foreign_fut_net_oi", "foreign_fut_oi_chg_5d",
    ]
    for col in CHIP_ZERO_COLS:
        if col in X.columns:
            X[col] = X[col].fillna(0)
    # 基本面特徵（PE/PEG/ROE/ni_yoy 等）NaN 保留
    # XGBoost 內建處理 NaN（自動學 split 方向），比用 median 填補更準
    # 虧損股 PE=NaN 被填 median=20 會誤導模型以為「估值正常」
    feature_medians = X.median()  # 仍計算供 bundle 儲存（相容舊 predict）
    y = combined["label"]

    pos_rate = y.mean()
    print(f"訓練資料：{len(X)} 筆，{combined['symbol'].nunique()} 檔，正例：{pos_rate:.2%}", flush=True)

    if len(X) < 200:
        print("資料不足（需至少200筆）", flush=True)
        return

    # 樣本不平衡：設定 scale_pos_weight（負例數/正例數）
    neg_count = int((y == 0).sum())
    pos_count = int((y == 1).sum())
    spw = neg_count / pos_count if pos_count > 0 else 1.0
    print(f"scale_pos_weight={spw:.2f}", flush=True)

    EMBARGO_DAYS = FORWARD_DAYS
    tscv = TimeSeriesSplit(n_splits=5)
    dates_sorted = np.array(sorted(combined["date"].unique()))

    # ───────────────────────────────────────────────
    # 通用 purged walk-forward helper
    # ───────────────────────────────────────────────
    def make_classifier():
        return xgb.XGBClassifier(
            n_estimators=400, max_depth=4, learning_rate=0.04,
            subsample=0.8, colsample_bytree=0.7,
            min_child_weight=5, reg_alpha=0.1, reg_lambda=1.0,
            scale_pos_weight=spw, eval_metric="auc",
            random_state=42, n_jobs=-1,
        )

    def make_ranker():
        return xgb.XGBRanker(
            n_estimators=400, max_depth=4, learning_rate=0.04,
            subsample=0.8, colsample_bytree=0.7,
            min_child_weight=5, reg_alpha=0.1, reg_lambda=1.0,
            objective="rank:pairwise", eval_metric=["ndcg@20", "auc"],
            random_state=42, n_jobs=-1,
        )

    def cv_classifier(name, cols):
        """對 classifier 跑 purged walk-forward，回傳 (mean_auc, mean_hit@20)"""
        print(f"\n== {name} classifier ({len(cols)} features) ==", flush=True)
        X_sub = X[cols]
        aucs, hits = [], []
        for fold, (tr_idx, vl_idx) in enumerate(tscv.split(dates_sorted)):
            cutoff = max(0, vl_idx[0] - EMBARGO_DAYS)
            purged = tr_idx[tr_idx < cutoff]
            if len(purged) == 0:
                continue
            tr_dates = set(dates_sorted[purged])
            vl_dates = set(dates_sorted[vl_idx])
            mt = combined["date"].isin(tr_dates)
            mv = combined["date"].isin(vl_dates)
            Xt, yt = X_sub[mt], y[mt]
            Xv, yv = X_sub[mv], y[mv]
            if len(Xt) < 100 or len(Xv) < 20:
                continue
            m = make_classifier()
            m.fit(Xt, yt, eval_set=[(Xv, yv)], verbose=False)
            p = m.predict_proba(Xv)[:, 1]
            aucs.append(roc_auc_score(yv, p))
            vdf = combined[mv].copy()
            vdf["p"] = p
            h = [g.nlargest(20, "p")["label"].mean() for d, g in vdf.groupby("date") if len(g) >= 20]
            hits.append(np.mean(h) if h else 0.0)
            print(f"  Fold {fold+1}: AUC={aucs[-1]:.4f} hit@20={hits[-1]:.2%}", flush=True)
        mean_auc = float(np.mean(aucs[-3:])) if len(aucs) >= 3 else float(np.mean(aucs)) if aucs else 0.55
        mean_hit = float(np.mean(hits[-3:])) if len(hits) >= 3 else float(np.mean(hits)) if hits else 0.20
        print(f"  {name}: 後 3 folds AUC={mean_auc:.4f}, hit@20={mean_hit:.2%}", flush=True)
        return mean_auc, mean_hit

    def cv_ranker(name, cols):
        """XGBRanker with query group = date"""
        print(f"\n== {name} ranker ({len(cols)} features) ==", flush=True)
        X_sub = X[cols]
        aucs, hits = [], []
        for fold, (tr_idx, vl_idx) in enumerate(tscv.split(dates_sorted)):
            cutoff = max(0, vl_idx[0] - EMBARGO_DAYS)
            purged = tr_idx[tr_idx < cutoff]
            if len(purged) == 0:
                continue
            tr_dates = set(dates_sorted[purged])
            vl_dates = set(dates_sorted[vl_idx])
            mt = combined["date"].isin(tr_dates)
            mv = combined["date"].isin(vl_dates)
            if mt.sum() < 100 or mv.sum() < 20:
                continue
            # Ranker 需要 group（每個 query = 每個交易日的樣本數）
            tr_df = combined[mt].sort_values("date")
            vl_df = combined[mv].sort_values("date")
            Xt = X_sub.loc[tr_df.index]
            yt = y.loc[tr_df.index]
            Xv = X_sub.loc[vl_df.index]
            yv = y.loc[vl_df.index]
            gt = tr_df.groupby("date").size().values
            gv = vl_df.groupby("date").size().values
            m = make_ranker()
            m.fit(Xt, yt, group=gt, eval_set=[(Xv, yv)], eval_group=[gv], verbose=False)
            p = m.predict(Xv)
            try:
                aucs.append(roc_auc_score(yv, p))
            except Exception:
                aucs.append(0.5)
            vdf = vl_df.copy()
            vdf["p"] = p
            h = [g.nlargest(20, "p")["label"].mean() for d, g in vdf.groupby("date") if len(g) >= 20]
            hits.append(np.mean(h) if h else 0.0)
            print(f"  Fold {fold+1}: AUC={aucs[-1]:.4f} hit@20={hits[-1]:.2%}", flush=True)
        mean_auc = float(np.mean(aucs[-3:])) if len(aucs) >= 3 else float(np.mean(aucs)) if aucs else 0.55
        mean_hit = float(np.mean(hits[-3:])) if len(hits) >= 3 else float(np.mean(hits)) if hits else 0.20
        print(f"  {name}: 後 3 folds AUC={mean_auc:.4f}, hit@20={mean_hit:.2%}", flush=True)
        return mean_auc, mean_hit

    # ───────────────────────────────────────────────
    # 跑 4 個模型
    # ───────────────────────────────────────────────
    main_auc, main_hit = cv_ranker("main_ranker", FEATURE_COLS)
    brk_auc, brk_hit = cv_classifier("breakout", BREAKOUT_FEATURES)
    val_auc, val_hit = cv_classifier("value", VALUE_FEATURES)
    chp_auc, chp_hit = cv_classifier("chip", CHIP_FEATURES)

    # ───────────────────────────────────────────────
    # 最終 fit 全資料
    # ───────────────────────────────────────────────
    print("\n訓練最終模型（全資料）...", flush=True)
    full_sorted = combined.sort_values("date")
    X_sorted = X.loc[full_sorted.index]
    y_sorted = y.loc[full_sorted.index]
    groups = full_sorted.groupby("date").size().values

    main_model = make_ranker()
    main_model.fit(X_sorted, y_sorted, group=groups, verbose=False)

    brk_model = make_classifier(); brk_model.fit(X[BREAKOUT_FEATURES], y, verbose=False)
    val_model = make_classifier(); val_model.fit(X[VALUE_FEATURES], y, verbose=False)
    chp_model = make_classifier(); chp_model.fit(X[CHIP_FEATURES], y, verbose=False)

    bundle = {
        "version": "v4_multi",
        "models": {
            "main": main_model,
            "breakout": brk_model,
            "value": val_model,
            "chip": chp_model,
        },
        "feature_cols": {
            "main": FEATURE_COLS,
            "breakout": BREAKOUT_FEATURES,
            "value": VALUE_FEATURES,
            "chip": CHIP_FEATURES,
        },
        "metrics": {
            "main": {"auc": main_auc, "hit_at_20": main_hit},
            "breakout": {"auc": brk_auc, "hit_at_20": brk_hit},
            "value": {"auc": val_auc, "hit_at_20": val_hit},
            "chip": {"auc": chp_auc, "hit_at_20": chp_hit},
        },
        # 向後相容：predict.py 舊介面讀 model/feature_cols/mean_auc
        "model": main_model,
        "mean_auc": main_auc,
        "feature_medians": feature_medians.to_dict(),
    }

    with open(MODEL_PATH, "wb") as f:
        pickle.dump(bundle, f)
    print(f"\n模型已儲存：{MODEL_PATH}", flush=True)
    print(f"  main_ranker:  AUC={main_auc:.4f}  hit@20={main_hit:.2%}", flush=True)
    print(f"  breakout_clf: AUC={brk_auc:.4f}  hit@20={brk_hit:.2%}", flush=True)
    print(f"  value_clf:    AUC={val_auc:.4f}  hit@20={val_hit:.2%}", flush=True)
    print(f"  chip_clf:     AUC={chp_auc:.4f}  hit@20={chp_hit:.2%}", flush=True)


if __name__ == "__main__":
    train()
