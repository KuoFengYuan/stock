"""
XGBoost 模型訓練
用法：python ml/train.py
輸出：ml/model.pkl
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

    # 計算 forward return（直接在各 symbol 的時序中 shift）
    print("計算標籤...", flush=True)
    labeled_parts = []

    for symbol, g in feat_df.groupby("symbol"):
        g = g.sort_values("date").copy()
        close = g.set_index("date")["return20d"]  # 用 return20d 作 proxy 不夠準，改直接查 DB
        # 從 DB 取 close
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
        labeled_parts.append(g2.reset_index())

    conn.close()

    if not labeled_parts:
        print("無有效標籤資料", flush=True)
        return

    combined = pd.concat(labeled_parts, ignore_index=True)

    # 過濾異常 forward_ret（±50% 以上視為除權息還原價或資料缺口污染）
    combined = combined[combined["forward_ret"].abs() <= 0.5]

    # 標籤：20 天後絕對正報酬（> 0%）
    combined["label"] = (combined["forward_ret"] > 0).astype(int)

    # 去最後 FORWARD_DAYS 個交易日（避免 label 洩漏）
    combined = combined.dropna(subset=["label", "forward_ret"])
    all_dates = sorted(combined["date"].unique())
    if len(all_dates) > FORWARD_DAYS:
        cutoff = all_dates[-FORWARD_DAYS]
        combined = combined[combined["date"] < cutoff]

    X = combined[FEATURE_COLS].astype(float)
    # 替換 inf 為 NaN，再用各特徵中位數填補
    X = X.replace([np.inf, -np.inf], np.nan)
    X = X.fillna(X.median())
    y = combined["label"]

    print(f"訓練資料：{len(X)} 筆，{combined['symbol'].nunique()} 檔，正例：{y.mean():.2%}", flush=True)

    if len(X) < 200:
        print("資料不足（需至少200筆）", flush=True)
        return

    # 時序交叉驗證
    tscv = TimeSeriesSplit(n_splits=3)
    auc_scores = []
    dates_sorted = np.array(sorted(combined["date"].unique()))

    model = xgb.XGBClassifier(
        n_estimators=300, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        eval_metric="logloss", random_state=42, n_jobs=-1,
    )

    for fold, (train_idx, val_idx) in enumerate(tscv.split(dates_sorted)):
        train_dates = set(dates_sorted[train_idx])
        val_dates = set(dates_sorted[val_idx])
        mask_train = combined["date"].isin(train_dates)
        mask_val = combined["date"].isin(val_dates)
        X_train, y_train = X[mask_train], y[mask_train]
        X_val, y_val = X[mask_val], y[mask_val]
        if len(X_train) < 50 or len(X_val) < 10:
            continue
        model.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)
        proba = model.predict_proba(X_val)[:, 1]
        auc = roc_auc_score(y_val, proba)
        auc_scores.append(auc)
        print(f"  Fold {fold+1}: AUC={auc:.4f}", flush=True)

    if auc_scores:
        print(f"平均 AUC: {np.mean(auc_scores):.4f}", flush=True)

    print("訓練最終模型...", flush=True)
    model.fit(X, y, verbose=False)

    with open(MODEL_PATH, "wb") as f:
        pickle.dump({"model": model, "feature_cols": FEATURE_COLS}, f)
    print(f"模型已儲存：{MODEL_PATH}", flush=True)

    importance = dict(zip(FEATURE_COLS, model.feature_importances_))
    top = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:10]
    print("Top 10 特徵重要性：")
    for k, v in top:
        print(f"  {k}: {v:.4f}")


if __name__ == "__main__":
    train()
