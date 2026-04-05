# Stock_A — 台股 AI 選股系統

整合 XGBoost 機器學習模型與多層規則引擎，每日自動從 TWSE 抓取上市股票資料，產出評分與買進訊號。Next.js Web 介面呈現推薦清單、個股 K 線圖、法人籌碼圖表。

> 僅處理上市股票（TSE / .TW），不含上櫃（OTC / .TWO）。

---

## 目錄結構

```
app/                              # Next.js App Router 前端
  page.tsx                        # 首頁：推薦清單 + 同步/分析按鈕 + SSE 進度條
  stocks/[symbol]/                # 個股頁：K 線圖 + 法人籌碼 + 財務表
    CandleChart.tsx               # lightweight-charts 多面板圖表（K線/量/外資/投信）
  settings/page.tsx               # 設定頁（訓練、回測、篩選條件）
  api/
    sync/route.ts                 # POST → SSE 串流，呼叫 ml/sync.py
    analyze/route.ts              # POST → 呼叫 rule_engine.py 或 predict.py
    recommendations/route.ts      # GET → 推薦清單（分頁 + 篩選）
    stocks/[symbol]/route.ts      # GET → 個股價格 + 法人 + 財報
    train/route.ts                # POST → 觸發模型訓練
    backtest/route.ts             # POST → 觸發規則回測
lib/
  db/                             # better-sqlite3 + Drizzle ORM + migration
  analysis/ml-runner.ts           # child_process.spawn / stream 執行 Python
ml/                               # Python ML 模組（conda env: stock）
  sync.py                         # 資料同步：TWSE API + FinMind + yfinance → SQLite
  features.py                     # 特徵工程：17 維特徵
  fundamentals.py                 # 基本面計算（共用模組）
  strategies.py                   # Piotroski F-Score / PEG / Minervini SEPA
  rule_engine.py                  # 規則引擎：五層乘數評分
  train.py                        # XGBoost 訓練（相對強勢標籤��
  predict.py                      # ML × 權重 + 規則 × 權重 混合預測
  backtest.py                     # 規則回測 → rule_scores.json
  stock_list.py                   # 上市股票清單
  sync_tags.py                    # AI 概念股標籤同步
  tests/                          # pytest 測試（38 tests）
data/stock.db                     # SQLite 資料庫
types/stock.ts                    # TypeScript 型別定義
```

---

## 系統架構

```
TWSE API            yfinance          FinMind API
(日K/法人/融資券)    (季度財報)         (月營收)
       │                │                 │
       └────────────────┴─────────────────┘
                        │
                  ml/sync.py (120 workers 並行)
                        │
                  SQLite stock.db
                        │
         ┌──────────────┼──────────────────┐
    backtest.py    rule_engine.py      train.py
         │              │                  │
  rule_scores.json  推薦清單           model.pkl
                        │                  │
                        └──────┬───────────┘
                           predict.py
                     (ML × 權重 + 規則 × 權重)
                               │
                        recommendations 表
                               │
                    Next.js API → 前端頁面
```

---

## 雙軌混合評分

```
最終分數 = ML 分數 × ML權重 + 規則分數 × 規則權重
ML 權重 = clamp((AUC - 0.50) / 0.20 × 0.80, 0, 0.80)
```

- ML 權重由模型交叉驗證 AUC 自動決定（AUC 越高 → ML 佔比越大）
- 規則引擎有一票否決權：規則判定 neutral → 最終一定是 neutral

### 動態門檻（大盤擇時）

```
buy_thresh   = 0.56 + (market_win_rate - 0.50) × 0.30
watch_thresh = 0.50 + (market_win_rate - 0.50) × 0.30
```

熊市（market_win_rate < 0.42）時自動提高門檻，減少誤推。

---

## 規則引擎（五層乘數架構）

最終分數 = `底分 × 估值乘數 × 營收乘數 × 籌碼乘數 + 技術微調`

### 前置過濾（直接 neutral 退出）
- TTM 淨利為負 / PE > 60 / 近一年跌超 60% / Piotroski <= 1 / 無基本面訊號

### 第一層：基本面品質 → 底分 + 遞減加成
- ROE >= 20%、營收 YoY > 5%、獲利 YoY > 10%、負債 < 50%、Piotroski >= 4
- 超額勝率排序後遞減加權（100% → 40% → 20% → 10%）

### 第二層：估值乘數（× 0.70 ~ 1.15）
- PE / PB / PEG / 殖利率 / 產業相對估值 / 追高風險懲罰

### 第三層：月營收乘數（× 1.00 ~ 1.15）
- 年增連 >= 6 月、加速成長、月增連 >= 3 月

### 第四層：籌碼乘數（× 0.75 ~ 1.15）
- 外資/投信 60 日淨買（門檻 500 張）、10 日買賣超背離、融資融券

### 第五層：技術面微調（± 0.06）
- Minervini SEPA 趨勢、RS 相對強度、RSI、回調、量價背離

---

## XGBoost 模型

| 項目 | 值 |
|------|-----|
| 特徵數 | 17（技術 8 + 基本面 7 + 籌碼 2） |
| 標籤 | 相對強勢：60 交易日報酬排全市場前 30% 且 > 0% |
| 除權息過濾 | 相鄰日跌 > 20% → 前 60 天 label 排除 |
| 不平衡處理 | scale_pos_weight = 負例數/正例數 |
| 交叉驗證 | 5 折 TimeSeriesSplit |
| 特徵填補 | 訓練集 median（存入 model.pkl，預測時重用） |
| 防洩漏 | 財報用保守公告日（Q1→5/15、Q2→8/14、Q3→11/14、Q4→次年4/30） |

### ML 特徵

| 類別 | 特徵 |
|------|------|
| 技術面 | rsi14, bb_pos, sma20_bias, sma60_bias, vol_ratio, return20d, return60d, atr_pct |
| 基本面 | eps_ttm, roe, debt_ratio, revenue_yoy, ni_yoy, pe_ratio, pb_ratio |
| 籌碼面 | margin_balance_chg, short_balance_chg |

> `foreign_net_60d` / `trust_net_60d` 因歷史覆蓋率僅 30% 已從 ML 特徵移除，改由規則引擎處理。

---

## 資料來源

| 資料 | 來源 | 單位 | 更新頻率 |
|------|------|------|---------|
| 日 K 線 | TWSE 個股月 API | 張（股÷1000） | 每日 |
| 三大法人 | TWSE T86 API | 股（DB 原值） | 每日 |
| 融資融券 | TWSE MI_MARGN API | 張 | 每日 |
| 季度財報 | yfinance | 元 | 每季 |
| 月營收 | FinMind API | 元 | 每月 |

**重要**：`institutional` 表存的是「股」，前端顯示時 `÷1000` 換算「張」。

---

## 資料庫 Schema

| 資料表 | 主要欄位 |
|--------|---------|
| `stocks` | symbol(PK), name, market(TSE), industry |
| `stock_prices` | symbol, date, OHLCV, adj_close；UNIQUE(symbol, date) |
| `financials` | symbol, year, quarter, revenue, net_income, eps, equity, total_assets, total_debt |
| `institutional` | symbol, date, foreign_net, trust_net, dealer_net, total_net（單位：股） |
| `margin_trading` | symbol, date, margin/short buy/sell/balance |
| `monthly_revenue` | symbol, year, month, revenue, yoy, mom |
| `recommendations` | symbol, date, score, signal(buy/watch/neutral), reasons_json, features_json |
| `settings` | key, value(JSON) |
| `sync_log` | type, status, records_count, started_at, finished_at |

---

## Web 介面

### 首頁（`/`）
- 推薦清單：代號、名稱、市場、收盤價、漲跌幅、成交量、評分條、訊號標籤、推薦理由
- AI 概念股標籤（紫色）+ 子分類
- 同步資料（SSE 即時進度條）/ 規則分析 / AI 分析
- 篩選：全部 / 買入 / 觀察 / 中立

### 個股頁（`/stocks/[symbol]`）
- K 線圖：紅漲綠跌，OHLC + 漲跌幅 tooltip
- 均線：MA5（黃）/ MA10（橙）/ MA20（青）/ MA60（紫），可切換
- 成交量柱狀圖
- 外資買賣超柱狀圖（藍/粉）+ 投信買賣超柱狀圖（綠/橙）
- 季度財務表（近 8 季）

### 設定頁（`/settings`）
- 模型狀態 + 重新訓練 / 規則回測
- 篩選條件：最低評分、PE/PB/ROE、成交量、市場、排除個股

---

## 開發環境

### 需求
- Node.js 20+
- Python 3.11+（conda env `stock`）

### 安裝

```bash
npm install
conda create -n stock python=3.12
conda activate stock
pip install -r ml/requirements.txt
```

### 啟動

```bash
npm run dev   # http://localhost:3000
```

### 測試

```bash
conda run -n stock python3 -m pytest ml/tests/ -v
```

38 個測試覆蓋：基本面計算、特徵工程、規則引擎、策略模組、資料完整性。

Claude Code 已設定 PostToolUse hook：每次 Edit/Write 自動跑測試。

### 首次使用

1. **同步資料**（首次約 10–30 分鐘，120 workers 並行）
2. **規則分析**（產生推薦清單）
3. 設定頁 → **訓練模型**（需 >= 200 筆有效資料）
4. **AI 分析**（ML + 規則混合推薦）

---

## 技術堆疊

| 層面 | 技術 |
|------|------|
| 前端 | Next.js 16（App Router）+ React 19 |
| 樣式 | Tailwind CSS v4（深色主題） |
| 圖表 | lightweight-charts 5.x（多面板同步） |
| 資料庫 | SQLite（better-sqlite3）+ Drizzle ORM |
| ML | XGBoost 2.1 + scikit-learn + pandas-ta |
| 策略 | Piotroski F-Score + PEG Ratio + Minervini SEPA |
| 資料來源 | TWSE、yfinance、FinMind |
| 並行 | ThreadPoolExecutor（價格 120 workers、財報 20 workers） |
| 測試 | pytest（38 tests）+ Claude Code hook 自動觸發 |
