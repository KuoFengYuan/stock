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
FORWARD_DAYS = 60   # 中長期：60 交易日（約 3 個月）
RELATIVE_TOP_PCT = 0.30   # 前30%相對強勢視為正例


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

    # ── 混合標籤：相對強勢前30% 且 絕對報酬 > 0 ──
    # 純相對標籤在熊市會把「虧少的」當正例，模型學到「跌少」而非「會漲」
    # 加入絕對報酬 > 0 的條件，確保正例真的賺錢
    print("計算混合標籤（相對強勢 + 絕對獲利）...", flush=True)
    threshold_per_day = combined.groupby("date")["forward_ret"].transform(
        lambda x: x.quantile(1 - RELATIVE_TOP_PCT)
    )
    combined["label"] = (
        (combined["forward_ret"] >= threshold_per_day) &
        (combined["forward_ret"] > 0)
    ).astype(int)
    combined = combined.dropna(subset=["label"])

    X = combined[FEATURE_COLS].astype(float)
    X = X.replace([np.inf, -np.inf], np.nan)
    # 用各特徵訓練集中位數填補 NaN（訓練時計算，預測時重用）
    feature_medians = X.median()
    X = X.fillna(feature_medians)
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

    tscv = TimeSeriesSplit(n_splits=5)
    auc_scores = []
    dates_sorted = np.array(sorted(combined["date"].unique()))

    model = xgb.XGBClassifier(
        n_estimators=400,
        max_depth=4,
        learning_rate=0.04,
        subsample=0.8,
        colsample_bytree=0.7,
        min_child_weight=5,     # 避免在小樣本分裂，降低過擬合
        reg_alpha=0.1,           # L1 正則
        reg_lambda=1.0,          # L2 正則
        scale_pos_weight=spw,
        eval_metric="auc",
        random_state=42,
        n_jobs=-1,
    )

    for fold, (train_idx, val_idx) in enumerate(tscv.split(dates_sorted)):
        train_dates = set(dates_sorted[train_idx])
        val_dates   = set(dates_sorted[val_idx])
        mask_train  = combined["date"].isin(train_dates)
        mask_val    = combined["date"].isin(val_dates)
        X_train, y_train = X[mask_train], y[mask_train]
        X_val,   y_val   = X[mask_val],   y[mask_val]
        if len(X_train) < 100 or len(X_val) < 20:
            continue
        model.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)
        proba = model.predict_proba(X_val)[:, 1]
        auc = roc_auc_score(y_val, proba)
        auc_scores.append(auc)
        print(f"  Fold {fold+1}: AUC={auc:.4f} (train={len(X_train)}, val={len(X_val)})", flush=True)

    mean_auc = float(np.mean(auc_scores)) if auc_scores else 0.60
    print(f"平均 AUC: {mean_auc:.4f}", flush=True)

    print("訓練最終模型...", flush=True)
    model.fit(X, y, verbose=False)

    with open(MODEL_PATH, "wb") as f:
        pickle.dump({
            "model": model,
            "feature_cols": FEATURE_COLS,
            "mean_auc": mean_auc,
            "feature_medians": feature_medians.to_dict(),   # 供預測時填補 NaN 用
        }, f)
    print(f"模型已儲存：{MODEL_PATH}", flush=True)

    importance = dict(zip(FEATURE_COLS, model.feature_importances_))
    top = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:10]
    print("Top 10 特徵重要性：")
    for k, v in top:
        print(f"  {k}: {v:.4f}")


if __name__ == "__main__":
    train()
