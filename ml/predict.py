"""
預測腳本：用訓練好的模型對最新資料評分，結果寫入 recommendations 表
改進：
1. ML 權重公式修正：AUC 0.55→30%, 0.65→65%，更合理反映模型價值
2. 推薦門檻改用 apply_rules 的動態門檻（與 rule_engine 一致）
3. 預測時特徵填補改用訓練集中位數（model bundle 存的），而非即時中位數
4. 預測時籌碼計算與訓練一致（rolling 60日 sum）
5. 大盤擇時傳入 predict，熊市提高門檻
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
from features import _calc_price_features, _get_fund_features
from fundamentals import calc_fundamentals
from strategies import calc_piotroski, calc_peg, calc_minervini
from rule_engine import calc_indicators, apply_rules, calc_monthly_revenue, _calc_high_1y, _calc_market_win_rate
from agents import apply_agents

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
    model_auc = bundle.get("mean_auc", 0.60)
    # 訓練集中位數（比即時中位數更穩定，避免因預測樣本少而偏移）
    feature_medians = bundle.get("feature_medians", {})

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    row = conn.execute("SELECT date FROM stock_prices ORDER BY date DESC LIMIT 1").fetchone()
    if not row:
        print("無價格資料", flush=True)
        conn.close()
        return
    latest_date = row["date"]
    print(f"預測日期：{latest_date}", flush=True)

    # 大盤擇時：計算市場近期勝率
    market_win_rate = _calc_market_win_rate(conn)
    market_env = "熊市" if market_win_rate < 0.42 else ("牛市" if market_win_rate > 0.55 else "正常")
    print(f"市場近期勝率：{market_win_rate:.1%}（{market_env}）", flush=True)

    # ML 權重：AUC 0.50→0%, 0.55→30%, 0.60→50%, 0.65→70%, 0.70+→80%
    # 線性插值：(AUC - 0.50) / 0.20 * 1.0，上限 0.80
    ml_weight = max(0.0, min(0.80, (model_auc - 0.50) / 0.20))
    rule_weight = 1.0 - ml_weight
    print(f"模型 AUC={model_auc:.4f}，ML權重={ml_weight:.2f}，規則權重={rule_weight:.2f}", flush=True)

    symbols = [r["symbol"] for r in conn.execute("SELECT symbol FROM stocks WHERE market='TSE'").fetchall()]
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

            price_feats = _calc_price_features(df)
            if price_feats.empty or len(price_feats.dropna(how="all")) == 0:
                continue

            latest_feat = price_feats.iloc[[-1]].copy()
            current_price = float(df["close"].iloc[-1])

            # 基本面
            fund = _get_fund_features(symbol, conn, price=current_price)
            for k, v in fund.items():
                latest_feat[k] = v

            # 籌碼：與訓練時一致，用 rolling 60日 sum
            inst_rows = conn.execute(
                "SELECT date, foreign_net, trust_net, total_net FROM institutional WHERE symbol=? ORDER BY date DESC LIMIT 65",
                (symbol,)
            ).fetchall()
            margin_rows = conn.execute(
                "SELECT date, margin_balance, short_balance FROM margin_trading WHERE symbol=? ORDER BY date DESC LIMIT 10",
                (symbol,)
            ).fetchall()

            if inst_rows:
                inst_df = pd.DataFrame(inst_rows, columns=["date", "foreign_net", "trust_net", "total_net"])
                # 60日累計（與訓練特徵一致）
                latest_feat["foreign_net_60d"] = inst_df["foreign_net"].head(60).sum()
                latest_feat["trust_net_60d"]   = inst_df["trust_net"].head(60).sum()
                # 10日用於籌碼警告（不進模型）
                latest_feat["foreign_net_10d"] = inst_df["foreign_net"].head(10).sum()
                latest_feat["trust_net_10d"]   = inst_df["trust_net"].head(10).sum()

            if margin_rows and len(margin_rows) >= 6:
                mg = pd.DataFrame(margin_rows, columns=["date", "margin_balance", "short_balance"])
                mb_now, mb_5 = mg["margin_balance"].iloc[0], mg["margin_balance"].iloc[5]
                sb_now, sb_5 = mg["short_balance"].iloc[0], mg["short_balance"].iloc[5]
                latest_feat["margin_balance_chg"] = (mb_now - mb_5) / mb_5 * 100 if mb_5 else 0
                latest_feat["short_balance_chg"]  = (sb_now - sb_5) / sb_5 * 100 if sb_5 else 0

            # 規則引擎 + 大師共識（先算，因為 agent_score 要進 ML 特徵）
            tech = calc_indicators(df)
            tech["high_1y"] = _calc_high_1y(df)
            if inst_rows:
                tech["foreign_net_60d"] = float(latest_feat["foreign_net_60d"].iloc[0]) if "foreign_net_60d" in latest_feat.columns else 0
                tech["trust_net_60d"]   = float(latest_feat["trust_net_60d"].iloc[0])   if "trust_net_60d"   in latest_feat.columns else 0
                tech["foreign_net_10d"] = float(latest_feat["foreign_net_10d"].iloc[0]) if "foreign_net_10d" in latest_feat.columns else 0
                tech["trust_net_10d"]   = float(latest_feat["trust_net_10d"].iloc[0])   if "trust_net_10d"   in latest_feat.columns else 0

            fund2 = calc_fundamentals(symbol, conn, price=current_price)
            monthly = calc_monthly_revenue(symbol, conn)
            reasons, rule_signal, rule_score = apply_rules(tech, fund2, current_price, monthly, market_win_rate)

            # 第六層：大師共識
            tag_rows = conn.execute(
                "SELECT tag, sub_tag FROM stock_tags WHERE symbol=?", (symbol,)
            ).fetchall()
            agent_ctx = {
                "fund": fund2, "tech": tech, "monthly": monthly,
                "tags": [{"tag": t[0], "sub_tag": t[1]} for t in tag_rows],
            }
            agent_result = apply_agents(agent_ctx)
            rule_score = max(0.0, min(1.0, rule_score + agent_result["bonus"]))
            if agent_result["consensus"]["bullish"] >= 5:
                reasons.append(f"大師共識 {agent_result['consensus']['bullish']}/7 看多")
            elif agent_result["consensus"]["bearish"] >= 5:
                reasons.append(f"大師共識 {agent_result['consensus']['bearish']}/7 看空")

            # 把 agent_score 和月營收特徵塞進 latest_feat
            latest_feat["agent_score"] = agent_result["agent_score"]
            latest_feat["rev_consecutive_yoy"] = monthly.get("rev_consecutive_yoy", 0) or 0
            latest_feat["rev_accel"] = 1.0 if monthly.get("rev_accel") else 0.0

            # PE/PB clip（與訓練一致）
            if "pe_ratio" in latest_feat.columns:
                latest_feat["pe_ratio"] = latest_feat["pe_ratio"].clip(lower=0, upper=200)
            if "pb_ratio" in latest_feat.columns:
                latest_feat["pb_ratio"] = latest_feat["pb_ratio"].clip(lower=0, upper=30)

            # 補齊缺失特徵
            for col in feature_cols:
                if col not in latest_feat.columns:
                    latest_feat[col] = np.nan

            X = latest_feat[feature_cols].astype(float)
            if X.isnull().all(axis=1).iloc[0]:
                continue

            # 用訓練集中位數填補（比即時中位數更穩定）
            if feature_medians:
                for col in feature_cols:
                    if pd.isna(X[col].iloc[0]) and col in feature_medians:
                        X[col] = feature_medians[col]
            X = X.fillna(0)  # 仍有殘餘 NaN 則填 0

            ml_score = float(model.predict_proba(X)[0, 1])

            # 混合評分
            final_score = ml_score * ml_weight + rule_score * rule_weight

            # 動態門檻
            buy_thresh   = 0.56 + (market_win_rate - 0.50) * 0.30
            watch_thresh = 0.50 + (market_win_rate - 0.50) * 0.30
            if market_win_rate < 0.42:
                buy_thresh   = max(buy_thresh, 0.58)
                watch_thresh = max(watch_thresh, 0.52)
            buy_thresh   = max(0.52, min(buy_thresh,   0.65))
            watch_thresh = max(0.46, min(watch_thresh, 0.58))

            # 規則引擎硬性排除（無 reasons = 真 neutral）→ ML 無法救
            # 有 reasons 的 neutral（被風險扣分壓低）→ ML 有機會拉回到 watch
            if rule_signal == "neutral" and not reasons:
                signal = "neutral"
                final_score = min(final_score, 0.30)
            elif rule_signal == "neutral" and reasons:
                # 有基本面但被扣分壓低，ML 最多拉到 watch（不給 buy）
                if final_score >= watch_thresh:
                    signal = "watch"
                else:
                    signal = "neutral"
            elif final_score >= buy_thresh:
                signal = "buy"
            elif final_score >= watch_thresh:
                signal = "watch"
            else:
                signal = "neutral"

            features_dict = {
                col: (float(X[col].iloc[0]) if not pd.isna(X[col].iloc[0]) else None)
                for col in feature_cols
            }
            features_dict["agent_score"] = agent_result["agent_score"]
            features_dict["agent_consensus"] = agent_result["consensus"]
            features_dict["agent_details"] = agent_result["details"]
            features_json = json.dumps(features_dict)

            conn.execute(
                """INSERT OR REPLACE INTO recommendations
                   (symbol, date, score, signal, features_json, reasons_json, model_version, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    symbol, latest_date, final_score, signal,
                    features_json, json.dumps(reasons),
                    "xgb_v3", int(time.time() * 1000)
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
