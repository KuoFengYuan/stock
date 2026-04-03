"""
預測腳本：用訓練好的模型對最新資料評分，結果寫入 recommendations 表
用法：python ml/predict.py
"""
import sys
import sqlite3
import pickle
import time
import json
from pathlib import Path

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from features import build_feature_matrix, FEATURE_COLS
from rule_engine import calc_indicators, calc_fundamentals, apply_rules

DB_PATH = Path(__file__).parent.parent / "data" / "stock.db"
MODEL_PATH = Path(__file__).parent / "model.pkl"


def run_predict():
    if not MODEL_PATH.exists():
        print("模型檔案不存在，請先執行 train.py", flush=True)
        sys.exit(1)

    with open(MODEL_PATH, "rb") as f:
        bundle = pickle.load(f)
    model = bundle["model"]
    feature_cols = bundle["feature_cols"]

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    # 取最新交易日
    row = conn.execute("SELECT date FROM stock_prices ORDER BY date DESC LIMIT 1").fetchone()
    if not row:
        print("無價格資料", flush=True)
        conn.close()
        return
    latest_date = row["date"]
    print(f"預測日期：{latest_date}", flush=True)

    symbols = [r["symbol"] for r in conn.execute("SELECT symbol FROM stocks").fetchall()]
    count = 0
    started_at = int(time.time() * 1000)

    for symbol in symbols:
        try:
            price_rows = conn.execute(
                "SELECT date, open, high, low, close, volume FROM stock_prices WHERE symbol=? ORDER BY date ASC",
                (symbol,)
            ).fetchall()

            if len(price_rows) < 60:
                continue

            df = pd.DataFrame([dict(r) for r in price_rows])
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")

            from features import _calc_price_features, _get_fund_features, _get_chip_features, _load_all_institutional, _load_all_margin
            price_feats = _calc_price_features(df)
            if price_feats.empty or len(price_feats.dropna(how="all")) == 0:
                continue

            latest_feat = price_feats.iloc[[-1]].copy()
            current_price = float(df["close"].iloc[-1])

            # 基本面（傳入股價以計算 PE/PB）
            fund = _get_fund_features(symbol, conn, price=current_price)
            for k, v in fund.items():
                latest_feat[k] = v

            # 籌碼（5日 + 20日累計）
            inst_rows = conn.execute(
                "SELECT date, foreign_net, trust_net, total_net FROM institutional WHERE symbol=? ORDER BY date DESC LIMIT 65",
                (symbol,)
            ).fetchall()
            margin_rows = conn.execute(
                "SELECT date, margin_balance, short_balance FROM margin_trading WHERE symbol=? ORDER BY date DESC LIMIT 10",
                (symbol,)
            ).fetchall()

            if inst_rows:
                inst_df = pd.DataFrame(inst_rows, columns=["date","foreign_net","trust_net","total_net"])
                latest_feat["foreign_net_60d"] = inst_df["foreign_net"].head(60).sum()
                latest_feat["trust_net_60d"]   = inst_df["trust_net"].head(60).sum()
                latest_feat["foreign_net_10d"] = inst_df["foreign_net"].head(10).sum()
                latest_feat["trust_net_10d"]   = inst_df["trust_net"].head(10).sum()
            if margin_rows and len(margin_rows) >= 6:
                mg = pd.DataFrame(margin_rows, columns=["date","margin_balance","short_balance"])
                mb_now, mb_5 = mg["margin_balance"].iloc[0], mg["margin_balance"].iloc[5]
                sb_now, sb_5 = mg["short_balance"].iloc[0], mg["short_balance"].iloc[5]
                latest_feat["margin_balance_chg"] = (mb_now - mb_5) / mb_5 * 100 if mb_5 else 0
                latest_feat["short_balance_chg"] = (sb_now - sb_5) / sb_5 * 100 if sb_5 else 0

            # 補齊缺失的特徵欄（確保所有 feature_cols 都存在）
            for col in feature_cols:
                if col not in latest_feat.columns:
                    latest_feat[col] = np.nan

            # 準備特徵向量
            X = latest_feat[feature_cols].astype(float)
            if X.isnull().all(axis=1).iloc[0]:
                continue

            # 用中位數填補 NaN
            X = X.fillna(X.median())

            ml_score = float(model.predict_proba(X)[0, 1])

            # 同時跑規則引擎取得理由
            tech = calc_indicators(df)
            from rule_engine import _calc_high_1y
            tech["high_1y"] = _calc_high_1y(df)
            fund2 = calc_fundamentals(symbol, conn)
            from rule_engine import calc_monthly_revenue
            monthly = calc_monthly_revenue(symbol, conn)
            close = float(df["close"].iloc[-1])
            reasons, rule_signal, rule_score = apply_rules(tech, fund2, close, monthly)

            # 動態 ML 權重：根據模型 AUC 決定 ML 比重
            # AUC 存在 bundle 中（訓練時寫入），若無則預設 0.60
            model_auc = bundle.get("mean_auc", 0.60)
            # AUC 0.50 = 隨機猜測，完全不可信；AUC 0.70+ = 有效模型
            # 線性映射：AUC 0.50 → ml_weight 0.0；AUC 0.70 → ml_weight 0.7
            ml_weight = max(0.0, min(0.7, (model_auc - 0.50) / 0.20 * 0.7))
            rule_weight = 1.0 - ml_weight

            # 混合評分
            final_score = ml_score * ml_weight + rule_score * rule_weight

            # 規則引擎 neutral（基本面不過關）→ 不論 ML 分數多高都不推薦
            if rule_signal == "neutral":
                signal = "neutral"
                final_score = 0.3
            elif final_score >= 0.56:
                signal = "buy"
            elif final_score >= 0.50:
                signal = "watch"
            else:
                signal = "neutral"

            # 籌碼警告：法人近10日同步賣超 → 強制降級（與 rule_engine 一致）
            foreign_10d = float(latest_feat["foreign_net_10d"].iloc[0]) if "foreign_net_10d" in latest_feat.columns else None
            trust_10d   = float(latest_feat["trust_net_10d"].iloc[0])   if "trust_net_10d"   in latest_feat.columns else None
            if foreign_10d is not None and trust_10d is not None:
                if foreign_10d < 0 and trust_10d < 0:
                    reasons.append("⚠ 法人近10日同步賣超")
                    if signal == "buy":
                        signal = "watch"
                    elif signal == "watch":
                        signal = "neutral"
                elif foreign_10d > 0 and trust_10d < 0:
                    f, t = abs(foreign_10d), abs(trust_10d)
                    if t > f * 3:
                        reasons.append("⚠ 投信大賣／外資小買（近10日）")
                        if signal == "buy":
                            signal = "watch"
                    else:
                        reasons.append("外資買超／投信賣超（近10日）")
                elif foreign_10d < 0 and trust_10d > 0:
                    f, t = abs(foreign_10d), abs(trust_10d)
                    if f > t * 3:
                        reasons.append("⚠ 外資大賣／投信小買（近10日）")
                        if signal == "buy":
                            signal = "watch"
                    else:
                        reasons.append("投信買超／外資賣超（近10日）")

            features_json = json.dumps({
                col: (float(X[col].iloc[0]) if not pd.isna(X[col].iloc[0]) else None)
                for col in feature_cols
            })

            conn.execute(
                """INSERT OR REPLACE INTO recommendations
                   (symbol, date, score, signal, features_json, reasons_json, model_version, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    symbol, latest_date, final_score, signal,
                    features_json, json.dumps(reasons),
                    "xgb_v2", int(time.time() * 1000)
                )
            )
            count += 1
            print(f"  {symbol}: ml={ml_score:.2f} rule={rule_score:.2f} final={final_score:.2f} {signal}", flush=True)

        except Exception as e:
            print(f"  [WARN] {symbol}: {e}", flush=True)

    conn.commit()
    conn.execute(
        "INSERT INTO sync_log (type, status, records_count, started_at, finished_at) VALUES (?,?,?,?,?)",
        ("analysis", "success", count, started_at, int(time.time() * 1000))
    )
    conn.commit()
    conn.close()

    print(f"\n預測完成，共 {count} 檔", flush=True)


if __name__ == "__main__":
    run_predict()
