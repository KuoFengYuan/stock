# Stock_A — 台股 AI 選股系統

整合 XGBoost 多模型集成與多層規則引擎，每日自動從 TWSE / TAIFEX / FinMind 抓取上市股票與期貨資料，產出評分與買進訊號。Next.js Web 介面呈現推薦清單、個股 K 線圖、推薦勝率追蹤。

> 僅處理上市股票（TSE / .TW），不含上櫃（OTC / .TWO）。

**線上版**：[https://claude.venraas.tw](https://claude.venraas.tw)

---

## 目錄結構

```
app/                              # Next.js App Router 前端
  page.tsx                        # 首頁：推薦清單 + 預設篩選 + 推薦勝率 + SSE 進度
  stocks/[symbol]/                # 個股頁：K 線圖 + 法人籌碼 + 新聞
    CandleChart.tsx               # lightweight-charts 多面板圖表
  settings/page.tsx               # 設定頁（訓練、回測、篩選）
  api/
    sync/route.ts                 # POST → SSE 串流，呼叫 ml/sync.py
    sync-status/route.ts          # GET → 各資料類型最後同步時間
    analyze/route.ts              # POST → rule_engine.py 或 predict.py
    recommendations/route.ts      # GET → 推薦清單
    performance/route.ts          # GET → 推薦追蹤勝率（1/3/5/20 日）
    stocks/[symbol]/route.ts      # GET → 個股價格 + 法人 + 財報
    feature-importance/route.ts   # GET → ML 特徵重要度
    train/route.ts                # POST → 觸發模型訓練
    backtest/route.ts             # POST → 觸發規則回測
lib/
  db/                             # better-sqlite3 + Drizzle ORM
  analysis/ml-runner.ts           # child_process spawn Python
ml/                               # Python ML 模組（conda env: stock）
  sync.py                         # TWSE + TAIFEX + MOPS + yfinance 同步
  sync_engine.py                  # 統一同步引擎（日期為單位、分類型 verify 補漏）
  features.py                     # 特徵工程：48 維（+ 除權息/財報窗口/產業相對/TAIFEX）
  fundamentals.py                 # 基本面計算（共用模組）
  strategies.py                   # Piotroski / PEG / Minervini SEPA
  rule_engine.py                  # 規則引擎：六層評分
  agents/                         # 投資大師 agent 模組（7 位）
  train.py                        # XGBoost 訓練（4 模型 + purged walk-forward）
  predict.py                      # Multi-model ensemble + Top-K 決策
  backtest.py                     # 規則回測 → rule_scores.json
  stock_list.py                   # 上市股票清單
  sync_tags.py                    # AI 概念股標籤同步
  tests/                          # pytest 測試（41 tests）
scripts/                          # 排程腳本
  daily.sh                        # 每日：sync + predict
  weekly.sh                       # 每週：sync + train + predict
  README.md                       # cron 設定教學
data/stock.db                     # SQLite 資料庫
types/stock.ts                    # TypeScript 型別定義
```

---

## 系統架構

```
TWSE API      TAIFEX API      FinMind        yfinance
(日K/法人/    (外資期貨       (月營收/       (季度財報)
 融資券)       未平倉)         單季EPS)
     │             │              │              │
     └─────────────┴──────────────┴──────────────┘
                          │
                   ml/sync.py
            (sync_engine: 日期為單位、分類型 verify)
                          │
                    SQLite stock.db
                          │
         ┌────────────────┼───────────────────┐
    backtest.py     rule_engine.py        train.py
         │               │                    │
    rule_scores.json   推薦清單        model.pkl (4 模型)
                         │            main / breakout /
                         │            value / chip
                         └──────┬───────────┘
                          predict.py
              (ML ensemble + 動態權重 + Top-K)
                                │
                        recommendations 表
                                │
                    Next.js API → 前端頁面
```

---

## 推薦邏輯（v4）

### 多模型集成

```
ml_score = main×w1 + breakout×w2 + value×w3 + chip×w4
final    = ml_score × ml_weight + rule_score × rule_weight
```

| 模型 | 類型 | 特徵子集 | 學習目標 |
|------|------|---------|---------|
| **main** | XGBRanker（pairwise） | 全 48 特徵 | 全市場每日排名 |
| **breakout** | XGBClassifier | 24 特徵（動能/型態/突破） | 突破型強勢 |
| **value** | XGBClassifier | 13 特徵（估值/基本面） | 價值回歸 |
| **chip** | XGBClassifier | 15 特徵（籌碼/事件窗口） | 主力動向 |

### 動態 Ensemble 權重

依大盤環境自動調整：

| 市場環境 | main | breakout | value | chip |
|---------|------|----------|-------|------|
| 熊市（win_rate < 42%） | 0.35 | 0.15 | **0.35** | 0.15 |
| 正常 | **0.40** | 0.25 | 0.20 | 0.15 |
| 牛市（win_rate > 55%） | 0.35 | **0.35** | 0.15 | 0.15 |

ML vs 規則權重：`ml_weight = clamp((AUC - 0.50) / 0.20 × 0.70, 0, 0.60)`

### Top-K 推薦

每日固定推薦數（取代門檻式）：
- **buy**：Top 20（超熊市縮至 Top 10）
- **watch**：Top 30
- 規則引擎仍有否決權（neutral 無 reasons → 強制 neutral）

### 動態門檻（保留作資格篩選）

```
buy_thresh   = 0.56 + (market_win_rate − 0.50) × 0.30
watch_thresh = 0.50 + (market_win_rate − 0.50) × 0.30
熊市加成：buy ≥ 0.58, watch ≥ 0.52
```

---

## 特徵工程（48 維）

| 類別 | 特徵 |
|------|------|
| 技術面（8） | rsi14, bb_pos, sma20_bias, sma60_bias, vol_ratio, return20d/60d, atr_pct |
| 動能/型態（7） | momentum_12_1, rs_pctile_60d, dist_from_52w_high, new_high_20d, consolidation_tight, breakout_with_volume, vol_surge |
| 價量訊號（3） | price_vol_bullish, distribution_flag, near_high_weak_rsi |
| 警告型（1） | vol_dry_down |
| 基本面（7） | eps_ttm, roe, debt_ratio, revenue_yoy, ni_yoy, pe_ratio, pb_ratio |
| 月營收（2） | rev_consecutive_yoy, rev_accel |
| 籌碼（7） | foreign_net_10d, trust_net_10d, both_inst_buying_10d, foreign_consec_buy, trust_consec_buy, margin_balance_chg, short_balance_chg |
| **事件窗口（4）** | ex_div_window, post_ex_div_recovery, near_earnings, earnings_drift |
| **產業相對（3）** | rs_vs_industry_20d, pe_pct_in_industry, industry_momentum |
| **宏觀（6）** | market_return_20d/60d, beta_60d, rel_strength_vs_mkt, foreign_fut_net_oi, foreign_fut_oi_chg_5d |

### 驗證策略
- **Purged Walk-Forward**：TimeSeriesSplit 5-fold + 20 天 embargo 避免 label 洩漏
- **評估指標**：AUC + hit@20（Top 20 正例命中率）
- **Lookahead 防護**：財報用保守公告日（Q1→5/15、Q2→8/14、Q3→11/14、Q4→次年4/30）

---

## 規則引擎六層

最終規則分 = `底分 + 基本面加成 + 估值調整 + 營收加成 + 籌碼調整 + 技術微調 + 大師共識`

### 前置過濾（直接 neutral）
- TTM 淨利為負 / PE > 100 / 近一年跌超 60% / Piotroski ≤ 1 / 近60日跌 > 20% + 均線空頭

### 六層細節
1. **基本面品質**：底分 + 遞減加成（ROE/營收/獲利/Piotroski），用回測 `_excess_win` 加權
2. **估值**：PE / PB / PEG / 殖利率 / 產業相對 / 追高懲罰
3. **月營收**：連 YoY / MoM、加速成長
4. **籌碼**：60 日淨買（500 張門檻）、10 日雙買（最強訊號）、連續買賣超、融資融券
5. **技術面**：Minervini SEPA、RS 百分位、量價背離、60 日新低
6. **大師共識**：7 位 agent（Buffett / Graham / Munger / Fisher / Druckenmiller / Wood / Ackman）

每位大師以規則式（非 LLM）評估，回 bullish/neutral/bearish + confidence + reasons。

---

## 資料來源

| 資料 | 來源 | 單位 | 更新頻率 |
|------|------|------|---------|
| 日 K 線 | TWSE MI_INDEX | 張（股÷1000） | 每日 |
| 三大法人 | TWSE T86 API | **股**（DB 原值） | 每日 |
| 融資融券 | TWSE MI_MARGN API | 張 | 每日 |
| **外資期貨 OI** | FinMind TaiwanFuturesInstitutionalInvestors | 口 | 每日 |
| 季度財報 | yfinance + FinMind（補 EPS） | 元 | 每季 |
| 月營收 | FinMind TaiwanStockMonthRevenue | 元 | 每月 10 日 |

**注意**：`institutional` 表存的是「股」，前端顯示時 `÷1000` 換算「張」。

---

## 資料庫 Schema

| 資料表 | 主要欄位 |
|--------|---------|
| `stocks` | symbol(PK), name, market(TSE), industry |
| `stock_prices` | symbol, date, OHLCV, adj_close |
| `financials` | symbol, year, quarter, revenue, net_income, eps, equity, debt |
| `institutional` | symbol, date, foreign_net, trust_net, dealer_net, total_net（股） |
| `margin_trading` | symbol, date, margin/short buy/sell/balance |
| `monthly_revenue` | symbol, year, month, revenue, yoy, mom |
| `futures_positions` | date(PK), foreign_long_oi, foreign_short_oi, foreign_net_oi |
| `recommendations` | symbol, date, score, signal, reasons_json, features_json（含 ml_sub_scores） |
| `stock_tags` | symbol, tag, sub_tag（AI 主題） |
| `sync_log` | type, status, records_count, started_at, finished_at |

---

# 📖 使用教學

## 初次設置

### 1. 安裝環境

```bash
# Node.js 相依
npm install

# Python 環境（conda env: stock）
conda create -n stock python=3.12
conda activate stock
pip install -r ml/requirements.txt
```

### 2. 首次資料同步（約 10-30 分鐘）

```bash
# 完整同步（價格/法人/融資券/財報/月營收/TAIFEX）
conda run -n stock python3 ml/sync.py all
```

或透過前端：啟動 dev server → 首頁按「同步資料」。

### 3. 訓練模型（需至少 200 筆資料）

```bash
conda run -n stock python3 ml/train.py
```

訓練 4 個模型（main ranker + breakout / value / chip），約 15 分鐘。

### 4. 產生推薦

```bash
conda run -n stock python3 ml/predict.py
```

或前端按「AI 分析」。

### 5. 啟動前端

```bash
./dev.sh     # 本機開發 (port 3000)
```

---

## 日常使用

### 自動排程（推薦）

一次設定，每天自動跑。編輯 crontab：

```bash
crontab -e
```

加入（把 `/path/to/stock_a` 換成實際路徑）：

```cron
# 每個工作日 19:00 同步 + 推薦
0 19 * * 1-5 cd /path/to/stock_a && ./scripts/daily.sh >> logs/cron.log 2>&1

# 每週六 12:00 重訓
0 12 * * 6   cd /path/to/stock_a && ./scripts/weekly.sh >> logs/cron.log 2>&1
```

建議 server 時區設為台灣：

```bash
sudo timedatectl set-timezone Asia/Taipei
```

詳細設定見 [`scripts/README.md`](scripts/README.md)。

### 手動執行

```bash
chmod +x scripts/daily.sh scripts/weekly.sh

./scripts/daily.sh      # 只跑同步 + 推薦
./scripts/weekly.sh     # 含重訓
```

Log 輸出在 `logs/`。

---

## 前端使用說明

### 首頁

#### 📊 推薦勝率追蹤（最上方）
- **Buy 表現**：近 90 天推薦的實際 forward return + 勝率
- 自動挑最長可用時窗（樣本 ≥10 才顯示長期）
- 展開看：1/3/5/20 日全部期間、4 個模型 Top 20 模擬績效
- **樣本累積中**：剛部署時只有 1 日資料，日後自然有 5/20 日

#### 訊號篩選
- `全部 / 買入 / 觀察 / 中立`

| 訊號 | 意義 |
|------|------|
| 🟢 買入（Top 20） | 多模型共識看好，ML + 規則雙強 |
| 🟡 觀察（Top 30） | 有亮點但尚未符合買入（追高、背離、規則警告） |
| ⚪ 中立 | 規則否決 或 分數不夠 |

#### 預設風格按鈕（4 個）

點下按鈕會同時「設 dim filter + 切換排序基準」：

| 按鈕 | 排序基準 | Filter 門檻 |
|------|---------|------------|
| ⚡ 動能派 Top 20 | `ml_sub_scores.breakout`（動能模型） | 動能 ≥ 70, 籌碼 ≥ 60, 基本面 ≥ 50 |
| 💎 價值派 Top 20 | `ml_sub_scores.value`（價值模型） | 基本面 ≥ 70, 估值 ≥ 60 |
| ⚖️ 均衡派 Top 20 | `final_score`（綜合 ensemble） | 基本面/動能/籌碼 各 ≥ 60 |
| 🏛 跟主力 Top 30 | `ml_sub_scores.chip`（籌碼模型） | 籌碼 ≥ 80 |

#### Slider 自訂篩選
- **基本面**：ROE、獲利、營收、Piotroski 綜合
- **動能**：RS 排名、趨勢、新高、回檔
- **籌碼**：外資/投信買賣超、融資融券
- **估值**：PE/PB/殖利率/PEG

#### 表格欄位說明

| 欄位 | 說明 |
|------|------|
| 股票 | 代號 + 名稱（AI 主題標紫色徽章） |
| 收盤 | 最新收盤價 |
| 漲跌 | **紅漲綠跌**（台股慣例） |
| 成交量 | 單位：張 |
| 評分 | 0-100 final_score 視覺化 |
| 維度 | 4 bar：基 / 動 / 籌 / 估（0-100 dim_scores） |
| **AI 模型** | 4 bar：綜 / 動 / 值 / 籌（ML 看多機率 0-1） |
| 訊號 | buy / watch / neutral |
| 大師 | 7 位大師看多數 X/7（hover 看細節） |
| 推薦理由 | 加減分訊號標籤 |

### 個股頁（`/stocks/[symbol]`）
- K 線圖：紅漲綠跌，OHLC + 量 + 外資 + 投信整合 tooltip
- 均線：MA5/10/20/60（可切換）
- 右側：季度財務 + 新聞
- 初始顯示一年（250 交易日）

### 設定頁（`/settings`）
- 模型狀態 + 重新訓練
- 規則回測 → 更新 rule_scores.json
- 篩選條件設定（評分/PE/成交量等）

---

## 如何驗證推薦效果

### 看「推薦勝率」區塊
- Buy 1 日平均 +X%、勝率 Y% → 越多樣本越可信
- 展開看 4 個模型分別的 Top 20 績效，找出最近最強的派系

### 比較「AI 模型」4 個 bar
- 4 個都高（> 0.6）→ 多模型共識，強
- 只有 1 個高 → 特定類型訊號，看自己偏好哪派
- main 高但其他低 → 綜合排名看好，但規則驅動成分較重

### 搭配「大師共識」交叉驗證
- 5/7 以上看多 + 4 個 ML 模型都高 → 最高信心
- 大師看空但 ML 看多 → 謹慎（通常 ML 吃短期動能，大師看長期）

---

## 測試

```bash
conda run -n stock python3 -m pytest ml/tests/ -v
```

**41 個測試**：基本面計算、特徵工程、規則引擎、策略模組、資料完整性。

每次 Edit/Write 會自動觸發 `pytest ml/tests/ -x -q`（Claude Code hook）。

---

## 技術堆疊

| 層面 | 技術 |
|------|------|
| 前端 | Next.js 16（App Router）+ React 19 |
| 樣式 | Tailwind CSS v4（深色主題） |
| 圖表 | lightweight-charts 5.x |
| 資料庫 | SQLite（better-sqlite3）+ Drizzle ORM |
| ML | XGBoost 2.1（Ranker + Classifier）+ scikit-learn + pandas-ta |
| 策略 | Piotroski F-Score + PEG + Minervini SEPA |
| 資料源 | TWSE / TAIFEX / yfinance / MOPS / FinMind |
| 並行 | ThreadPoolExecutor（sync_engine：15 workers / day-batch） |
| 測試 | pytest（41 tests）+ Claude Code PostToolUse hook |
| 排程 | cron + bash |
| 部署 | GCP VM (Linux) + nginx + Let's Encrypt SSL |

---

## 開發命令速查

```bash
# 資料 + 模型
conda run -n stock python3 ml/sync.py all            # 全量同步
conda run -n stock python3 ml/sync.py taifex         # 只同步 TAIFEX
conda run -n stock python3 ml/train.py               # 訓練 4 模型
conda run -n stock python3 ml/predict.py             # 產生推薦
conda run -n stock python3 ml/backtest.py            # 規則回測

# 測試
conda run -n stock python3 -m pytest ml/tests/ -v
conda run -n stock python3 -m pytest ml/tests/ -x -q --ignore=ml/tests/test_data_integrity.py

# 前端
npm run dev                                           # dev server
npm run build                                         # production build
npm run lint                                          # ESLint
npx tsc --noEmit                                      # 型別檢查

# 排程
./scripts/daily.sh                                    # 手動跑每日
./scripts/weekly.sh                                   # 手動跑每週（含重訓）
crontab -l                                            # 檢視已排程工作
tail -f logs/daily-*.log                              # 追蹤最新 log
```
