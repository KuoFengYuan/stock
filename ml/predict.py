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
from rule_engine import calc_indicators, apply_rules, calc_monthly_revenue, _calc_high_1y, _calc_market_win_rate, calc_dim_scores
from agents import apply_agents

DB_PATH = Path(__file__).parent.parent / "data" / "stock.db"
MODEL_PATH = Path(__file__).parent / "model.pkl"


def run_predict():
    if not MODEL_PATH.exists():
        print("模型檔案不存在，請先執行 train.py", flush=True)
        sys.exit(1)

    with open(MODEL_PATH, "rb") as f:
        bundle = pickle.load(f)

    # 新版 multi-model bundle（v4_multi）
    multi_mode = bundle.get("version") == "v4_multi" and "models" in bundle
    if multi_mode:
        models = bundle["models"]  # {"main", "breakout", "value", "chip"}
        feat_map = bundle["feature_cols"]  # dict per model
        metrics = bundle.get("metrics", {})
        # 全 feature 聯集（用於後續填補）
        feature_cols = feat_map["main"]
        model_auc = metrics.get("main", {}).get("auc", 0.60)
        print(f"[multi-model v4] main AUC={model_auc:.3f}, hit@20 main={metrics.get('main',{}).get('hit_at_20',0):.1%} breakout={metrics.get('breakout',{}).get('hit_at_20',0):.1%} value={metrics.get('value',{}).get('hit_at_20',0):.1%} chip={metrics.get('chip',{}).get('hit_at_20',0):.1%}", flush=True)
    else:
        # 舊版單一模型
        models = {"main": bundle["model"]}
        feat_map = {"main": bundle["feature_cols"]}
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

    # ML 權重：v4 多模型時上限提至 0.60（ensemble 較穩），舊模型仍 0.55
    # AUC 0.50→0%, 0.55→20%, 0.60→35%, 0.65→50%, 0.70+→60%
    ml_cap = 0.60 if multi_mode else 0.55
    ml_weight = max(0.0, min(ml_cap, (model_auc - 0.50) / 0.20 * 0.70))
    rule_weight = 1.0 - ml_weight
    print(f"模型 AUC={model_auc:.4f}，ML權重={ml_weight:.2f}，規則權重={rule_weight:.2f}", flush=True)

    symbols = [r["symbol"] for r in conn.execute("SELECT symbol FROM stocks WHERE market='TSE'").fetchall()]
    pending = []  # Top-K 決策 buffer
    started_at = int(time.time() * 1000)

    # 預先計算全市場當日 return60d 的百分位（rs_pctile_60d）
    cur = conn.execute(
        "SELECT symbol, close FROM stock_prices WHERE date = ?", (latest_date,)
    ).fetchall()
    # 取每檔 60 日前收盤
    prev_date_row = conn.execute(
        "SELECT DISTINCT date FROM stock_prices ORDER BY date DESC LIMIT 61"
    ).fetchall()
    prev_date = prev_date_row[-1]["date"] if len(prev_date_row) >= 61 else None
    rs_map = {}
    if prev_date:
        prev_closes = {r["symbol"]: r["close"] for r in conn.execute(
            "SELECT symbol, close FROM stock_prices WHERE date = ?", (prev_date,)
        ).fetchall()}
        rets = []
        for r in cur:
            p_prev = prev_closes.get(r["symbol"])
            if p_prev and p_prev > 0:
                rets.append((r["symbol"], (r["close"] / p_prev - 1) * 100))
        if rets:
            sorted_rets = sorted(rets, key=lambda x: x[1])
            n = len(sorted_rets)
            for i, (sym, _) in enumerate(sorted_rets):
                rs_map[sym] = (i + 1) / n * 100

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

            # 月營收特徵塞進 latest_feat（agent_score 已不是 ML 特徵，只在規則分用）
            latest_feat["rev_consecutive_yoy"] = monthly.get("rev_consecutive_yoy", 0) or 0
            latest_feat["rev_accel"] = 1.0 if monthly.get("rev_accel") else 0.0

            # rs_pctile_60d：全市場 return60d 百分位
            latest_feat["rs_pctile_60d"] = rs_map.get(symbol, 50.0)

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

            # 多模型 ensemble
            if multi_mode:
                sub_scores = {}
                for m_name, m_obj in models.items():
                    m_cols = feat_map[m_name]
                    # 確保所有欄位存在
                    X_m = X.reindex(columns=m_cols).fillna(0)
                    if m_name == "main":
                        # XGBRanker 回 raw score，sigmoid 轉 [0,1]
                        raw = float(m_obj.predict(X_m)[0])
                        sub_scores[m_name] = 1.0 / (1.0 + np.exp(-raw))
                    else:
                        sub_scores[m_name] = float(m_obj.predict_proba(X_m)[0, 1])
                # 動態 ensemble 權重：依大盤環境調整各模型重要性
                # 熊市 (win_rate<0.42): value 加重（估值安全墊）、breakout 降低（追高危險）
                # 牛市 (win_rate>0.55): breakout 加重（主升段）、value 降低
                # 正常: 平衡配置
                if market_win_rate < 0.42:
                    w_main, w_brk, w_val, w_chp = 0.35, 0.15, 0.35, 0.15
                elif market_win_rate > 0.55:
                    w_main, w_brk, w_val, w_chp = 0.35, 0.35, 0.15, 0.15
                else:
                    w_main, w_brk, w_val, w_chp = 0.40, 0.25, 0.20, 0.15
                ml_score = (
                    sub_scores["main"] * w_main +
                    sub_scores.get("breakout", 0.0) * w_brk +
                    sub_scores.get("value", 0.0) * w_val +
                    sub_scores.get("chip", 0.0) * w_chp
                )
                sub_scores["weights"] = {"main": w_main, "breakout": w_brk, "value": w_val, "chip": w_chp}
            else:
                ml_score = float(models["main"].predict_proba(X)[0, 1])
                sub_scores = {"main": ml_score}

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

            rs_val = rs_map.get(symbol)
            has_strong_momentum = (
                (rs_val is not None and rs_val >= 80) or
                agent_result["consensus"]["bullish"] >= 5
            )

            features_dict = {
                col: (float(X[col].iloc[0]) if not pd.isna(X[col].iloc[0]) else None)
                for col in feature_cols
            }
            features_dict["agent_score"] = agent_result["agent_score"]
            features_dict["agent_consensus"] = agent_result["consensus"]
            features_dict["agent_details"] = agent_result["details"]
            features_dict["ml_sub_scores"] = sub_scores

            # 5 維度分數
            dim_scores = calc_dim_scores(
                tech, fund2, current_price, monthly=monthly,
                minervini=None, rs_pctile=rs_val,
                agent_result=agent_result,
            )
            features_dict["dim_scores"] = dim_scores

            # 暫存 buffer，等全部算完再 Top-K 決定 signal
            pending.append({
                "symbol": symbol,
                "final_score": final_score,
                "ml_score": ml_score,
                "rule_score": rule_score,
                "rule_signal": rule_signal,
                "reasons": reasons,
                "features_dict": features_dict,
                "has_strong_momentum": has_strong_momentum,
                "buy_thresh": buy_thresh,
                "watch_thresh": watch_thresh,
            })

        except Exception as e:
            print(f"  [WARN] {symbol}: {e}", flush=True)

    # ═════════════════════════════════════════
    # Top-K 決策：先決定「資格」，再取分數前 K 為 buy
    # ═════════════════════════════════════════
    # 資格規則：
    # - 規則 neutral 無 reasons → 強制 neutral（不進 Top-K）
    # - 規則 neutral 有 reasons 無強勢 → 最多 watch（不進 Top-K）
    # - 其他 → 進入 Top-K 排名池
    TOP_K_BUY = 20      # 每日固定推前 20 檔
    TOP_K_WATCH = 30    # 再 30 檔 watch（total 50）
    # 熊市減量（系統性風險時少推薦）
    if market_win_rate < 0.35:
        TOP_K_BUY = 10
        TOP_K_WATCH = 20

    qualified = []
    for p in pending:
        if p["rule_signal"] == "neutral" and not p["reasons"]:
            p["signal"] = "neutral"
            p["final_score"] = min(p["final_score"], 0.30)
        elif p["rule_signal"] == "neutral" and p["reasons"] and not p["has_strong_momentum"]:
            # 最多 watch（但不進 buy 池）
            p["signal"] = "watch_candidate"
            qualified.append(p)
        else:
            p["signal"] = "candidate"
            qualified.append(p)

    # 排序（分數高到低）取 Top-K
    qualified.sort(key=lambda x: x["final_score"], reverse=True)
    # 先篩可 buy 的（signal=candidate 且 score >= buy_thresh 才能當 buy）
    buy_picked = 0
    watch_picked = 0
    for p in qualified:
        if p["signal"] == "candidate" and p["final_score"] >= p["buy_thresh"] and buy_picked < TOP_K_BUY:
            p["signal"] = "buy"
            buy_picked += 1
        elif p["final_score"] >= p["watch_thresh"] and watch_picked < TOP_K_WATCH:
            p["signal"] = "watch"
            watch_picked += 1
        else:
            p["signal"] = "neutral"

    print(f"\n[Top-K] 推薦 buy={buy_picked}/{TOP_K_BUY} watch={watch_picked}/{TOP_K_WATCH}（市場勝率 {market_win_rate:.0%}）", flush=True)

    # 批次寫入
    count = 0
    for p in pending:
        sig = p.get("signal", "neutral")
        conn.execute(
            """INSERT OR REPLACE INTO recommendations
               (symbol, date, score, signal, features_json, reasons_json, model_version, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                p["symbol"], latest_date, p["final_score"], sig,
                json.dumps(p["features_dict"]), json.dumps(p["reasons"]),
                "xgb_v4_multi" if multi_mode else "xgb_v3", int(time.time() * 1000)
            )
        )
        count += 1

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
