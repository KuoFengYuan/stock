# Stock_A — 台股 AI 選股系統

本系統整合機器學習（XGBoost）與規則引擎，每日自動從台灣證交所（TWSE）、櫃買中心（TPEX）抓取資料，產出個股評分與買進訊號，並透過 Next.js Web 介面呈現。

---

## 目錄結構

```
stock_a/
├── app/
│   ├── api/
│   │   ├── analyze/route.ts        # 觸發規則分析或 ML 推論
│   │   ├── backtest/route.ts       # 規則回測（GET 查詢結果 / POST 執行）
│   │   ├── filters/route.ts        # 篩選條件 CRUD
│   │   ├── model-status/route.ts   # ML 模型狀態查詢
│   │   ├── recommendations/route.ts# 推薦清單（分頁 + 篩選）
│   │   ├── stocks/[symbol]/route.ts# 個股詳情（價格、財務、法人）
│   │   ├── sync/route.ts           # 資料同步（含財報頻率限制）
│   │   └── train/route.ts          # 觸發模型重訓
│   ├── stocks/[symbol]/
│   │   ├── page.tsx                # 個股頁面（K 線圖、均線、財務表）
│   │   └── CandleChart.tsx         # lightweight-charts 多面板圖表
│   ├── settings/page.tsx           # 設定頁（訓練、回測、篩選條件）
│   ├── page.tsx                    # 首頁（推薦清單 + 操作按鈕）
│   └── layout.tsx                  # 根布局（導覽列）
├── lib/
│   ├── analysis/ml-runner.ts       # child_process.spawn 執行 Python 腳本
│   └── db/
│       ├── index.ts                # better-sqlite3 singleton 連線
│       ├── migrate.ts              # 資料庫 Schema 初始化（9 張表）
│       └── schema.ts               # Drizzle ORM 型別定義
├── ml/
│   ├── sync.py                     # 資料同步（TWSE / TPEX / yfinance / MOPS）
│   ├── features.py                 # 特徵工程（19 個特徵，防洩漏）
│   ├── train.py                    # XGBoost 模型訓練
│   ├── predict.py                  # ML + 規則混合評分推論
│   ├── rule_engine.py              # 純規則評分引擎
│   ├── backtest.py                 # 規則回測（產出 rule_scores.json）
│   ├── stock_list.py               # 股票清單定義
│   ├── tw_names.py                 # 中文股名對照表
│   ├── model.pkl                   # 訓練完成的 XGBoost 模型
│   ├── rule_scores.json            # 回測後各規則勝率與超額報酬
│   └── requirements.txt
├── data/
│   └── stock.db                    # SQLite 資料庫（~120 MB）
└── types/
    └── stock.ts                    # TypeScript 型別定義
```

---

## 系統架構

### 整體資料流

```
TWSE / TPEX API   yfinance（財報）   MOPS（月營收）
       │                 │                 │
       └─────────────────┴─────────────────┘
                         │
                   ml/sync.py
                         │
                   SQLite stock.db
    ┌────────────────────┼──────────────────────┐
 stocks           stock_prices             financials
 institutional    margin_trading           monthly_revenue
 recommendations  settings                sync_log
                         │
          ┌──────────────┼──────────────────┐
     train.py       rule_engine.py      predict.py
          │                │                │
      model.pkl       recommendations   recommendations
                      (規則評分)        (ML+規則混合)
```

---

## 資料同步（`ml/sync.py`）

### 資料來源

| 資料 | 來源 |
|------|------|
| 上市日 K | TWSE `STOCK_DAY` 逐月 API（50 workers 並行） |
| 上櫃當日 | TPEX 當日收盤 API |
| 季度財報 | yfinance（20 workers 並行） |
| 上市三大法人 | TWSE T86 fund flow API |
| 上櫃三大法人 | TPEX 3-institutional API |
| 融資融券 | TWSE `MI_MARGN` API |
| 每月營收 | MOPS `t05st10_ifrs`（HTML 解析） |

### 同步策略

- **首次 / 補資料**：若 DB 中有 120 天以上資料的股票低於 50%，觸發全量重載（2 年歷史）
- **增量更新**：只補 DB 最新日期到今天的缺口
- **財報限流**：7 天最短同步間隔（API 呼叫成本高）
- **並行下載**：Worker 只負責 HTTP fetch，主執行緒統一 `executemany` 寫入，避免 SQLite 並發衝突

### API 欄位對照

**TWSE T86 三大法人（19 欄）：**

| 索引 | 欄位 |
|------|------|
| `[4]` | 外資（不含陸資）淨買 |
| `[10]` | 投信淨買 |
| `[11]` | 自營商合計淨買 |
| `[18]` | 三大法人合計淨買 |

**TPEX 三大法人（24 欄）：**

| 索引 | 欄位 |
|------|------|
| `[4]` | 外資淨買 |
| `[19]` | 投信淨買 |
| `[22]` | 自營商合計淨買 |
| `[23]` | 三大法人合計淨買 |

---

## 特徵工程（`ml/features.py`）

共 **19 個特徵**，所有特徵以各自中位數填補 NaN：

### 技術面（8 個）

| 特徵 | 說明 |
|------|------|
| `rsi14` | RSI(14) |
| `bb_pos` | 布林帶位置（0–1） |
| `sma20_bias` | (收盤 − SMA20) / SMA20 × 100 |
| `sma60_bias` | (收盤 − SMA60) / SMA60 × 100 |
| `vol_ratio` | 當日量 / 20 日均量 |
| `return20d` | 20 日報酬% |
| `return60d` | 60 日報酬% |
| `atr_pct` | ATR / 收盤 × 100 |

### 基本面（7 個）

| 特徵 | 說明 |
|------|------|
| `eps_ttm` | 近四季 EPS 合計 |
| `roe` | TTM 淨利 / 股東權益 × 100 |
| `debt_ratio` | 總負債 / 總資產 × 100 |
| `revenue_yoy` | 營收 YoY% |
| `ni_yoy` | 淨利 YoY% |
| `pe_ratio` | 股價 / EPS TTM |
| `pb_ratio` | 股價 / 每股淨值 |

### 籌碼面（4 個）

| 特徵 | 說明 |
|------|------|
| `foreign_net_20d` | 60 日外資累積淨買（萬張） |
| `trust_net_20d` | 60 日投信累積淨買（萬張） |
| `margin_balance_chg` | 融資餘額 5 日變化% |
| `short_balance_chg` | 融券餘額 5 日變化% |

> **防未來資料洩漏**：財報依正式公告日對齊（Q1→5/15、Q2→8/14、Q3→11/14、Q4→3/31 隔年）

---

## 模型訓練（`ml/train.py`）

| 項目 | 值 |
|------|-----|
| 預測目標 | 60 個交易日後報酬 > 0%（二元分類） |
| 異常過濾 | \|報酬\| > 50% 排除（除權息污染） |
| 洩漏保護 | 排除最後 60 個交易日 |
| 最低資料量 | 200 筆 |
| 演算法 | XGBoost（n_estimators=300, depth=4, lr=0.05, subsample=0.8） |
| 驗證方式 | 3 折時序交叉驗證（TimeSeriesSplit），報告各折 AUC |
| 輸出 | `model.pkl`：`{"model": xgb, "feature_cols": [...]}` |

---

## 混合評分推論（`ml/predict.py`）

```
最終評分 = ML 機率分數 × 0.7 + 規則評分 × 0.3
```

| 訊號 | 條件 |
|------|------|
| `buy` | 評分 ≥ 0.55 |
| `watch` | 評分 ≥ 0.50 |
| `neutral` | 評分 < 0.50 |

所需歷史：最少 **60 筆**收盤價；特徵 NaN 以中位數填補

---

## 規則引擎（`ml/rule_engine.py`）

以「中期投資（3–12 個月）」為目標，各層規則得分加總後取平均：

### 基本面（必要條件）

| 條件 | 分數 |
|------|------|
| ROE ≥ 20% | 0.65 |
| ROE 12–20% | 0.57 |
| 營收 YoY > 5% | 0.58 |
| 淨利 YoY > 10% | 0.62 |
| 負債比 < 50% | 0.56 |

無任何基本面訊號 → 強制 `neutral`（score = 0.3）

### 估值

| 條件 | 分數 |
|------|------|
| PE < 15 | 0.60 |
| PE 15–25 | 0.54 |
| PE > 30 | 0.42（負向） |
| PB < 2 | 0.56 |
| PB > 4 | 0.44（負向） |

### 每月營收

| 條件 | 分數 |
|------|------|
| 連續 ≥ 6 個月 YoY 正成長 | 0.65 |
| 連續 3–5 個月 YoY 正成長 | 0.60 |
| 連續 ≥ 3 個月 MoM 正成長 | 0.58 |
| YoY 加速成長（本月 > 上月） | 0.61 |

### 籌碼面

| 條件 | 分數 |
|------|------|
| 外資 60 日累積淨買 > 0 | 0.57 |
| 投信 60 日累積淨買 > 0 | 0.56 |

### 技術面（進場時機）

| 條件 | 分數 |
|------|------|
| RSI < 35（超賣） | 0.57 |
| RSI > 75（超買） | 0.44（負向） |
| 股價 ≥ SMA60 × 0.95 | 0.53 |
| 20 日報酬 −10%~0%（回檔） | 0.54 |

**最終訊號**：平均分 ≥ 0.54 → `buy`｜≥ 0.48 → `watch`｜< 0.48 → `neutral`

**規則抑制**：讀取 `rule_scores.json`，回測勝率低於市場基準（預設 45%）的規則不計入評分

---

## 規則回測（`ml/backtest.py`）

| 項目 | 值 |
|------|-----|
| 預測窗口 | 60 個交易日（預設） |
| 最低樣本數 | 30 筆 |
| 基本面去重窗口 | 60 天（避免同檔重複觸發） |
| 異常過濾 | \|報酬\| > 50% 排除 |
| 輸出 | `rule_scores.json`：各規則 win_rate / excess_win_rate / avg_excess_return / sample_count |

---

## 資料庫 Schema

| 資料表 | 主要欄位 |
|--------|---------|
| `stocks` | symbol, name, market, industry |
| `stock_prices` | symbol, date, open, high, low, close, volume；唯一索引 (symbol, date) |
| `financials` | symbol, year, quarter, revenue, net_income, eps, equity, total_assets, total_debt |
| `institutional` | symbol, date, foreign_net, trust_net, dealer_net, total_net |
| `margin_trading` | symbol, date, margin_buy/sell/balance, short_buy/sell/balance |
| `monthly_revenue` | symbol, year, month, revenue, yoy, mom |
| `recommendations` | symbol, date, score, signal, features_json, reasons_json |
| `settings` | key, value（JSON） |
| `sync_log` | type, status, records_count, started_at, finished_at |

---

## API 端點

| 端點 | 方法 | 說明 |
|------|------|------|
| `/api/sync` | POST | 同步資料（mode: prices/financials/all；财报 7 天限流） |
| `/api/analyze` | POST | 執行分析（mode: rule/ml） |
| `/api/train` | POST | 訓練 XGBoost 模型 |
| `/api/backtest` | GET/POST | 查詢/執行規則回測 |
| `/api/recommendations` | GET | 推薦清單（date, limit, offset, 篩選條件） |
| `/api/stocks/[symbol]` | GET | 個股詳情（價格 120 筆、財報 8 季、法人全量） |
| `/api/filters` | GET/POST | 篩選條件讀寫 |
| `/api/model-status` | GET | 模型狀態（是否存在、新資料筆數、建議重訓） |

**模型重訓閾值**：訓練後新增 ≥ **5,000** 筆收盤價時，建議重訓

---

## Web 介面

### 首頁（`/`）

- 推薦清單：代號、名稱、市場、收盤價、漲跌幅%、成交量、評分進度條（≥70% 綠/≥50% 黃）、訊號標籤、推薦理由
- **同步資料** → `/api/sync?mode=all`
- **規則分析** → `rule_engine.py`
- **AI 分析** → `predict.py`（若無 model.pkl 則提示先訓練）
- 即時顯示 Python stdout log 與經過時間

### 個股頁面（`/stocks/[symbol]`）

- **K 線圖**：紅漲綠跌，最近 120 個交易日
- **均線**：MA5（黃）、MA10（橙）、MA20（青）、MA60（紫），可個別切換
- **外資買賣超**：藍（買超）/ 粉（賣超）柱狀圖，hover 顯示當日張數
- **投信買賣超**：綠（買超）/ 橙（賣超）柱狀圖，hover 顯示當日張數
- **季度財務表**：近 8 季營收（億）、淨利（億）、EPS

### 設定頁（`/settings`）

- 模型狀態：是否已訓練、訓練後新增價格筆數、是否建議重訓
- **重新訓練模型**
- **規則回測**：顯示各規則勝率、超額報酬表
- **篩選條件**：最低評分、最大 PE/PB、最低 ROE、最低成交量、市場（TSE/OTC）、排除個股

---

## 開發環境

### 需求

- Node.js 20+
- Python 3.11+

### 安裝

```bash
npm install
pip install -r ml/requirements.txt
```

### 啟動

```bash
npm run dev   # http://localhost:3000
```

### 首次使用

1. **同步資料**（首次約 10–30 分鐘）
2. **規則分析**（產生推薦清單）
3. 設定頁 → **重新訓練模型**（需 ≥ 200 筆有效訓練資料）
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
| 資料來源 | TWSE、TPEX、yfinance、MOPS |
| 並行 | ThreadPoolExecutor（價格 50 workers、財報 20 workers） |
