# Stock_A — 台股 AI 選股系統

整合 XGBoost 機器學習模型與多層規則引擎，每日自動從 TWSE 抓取上市股票資料，產出評分與買進訊號。Next.js Web 介面呈現推薦清單、個股 K 線圖、法人籌碼圖表。

> 僅處理上市股票（TSE / .TW），不含上櫃（OTC / .TWO）。

**線上版**：[https://claude.venraas.tw](https://claude.venraas.tw)

---

## 目錄結構

```
app/                              # Next.js App Router 前端
  page.tsx                        # 首頁：推薦清單 + 同步/分析按鈕 + SSE 進度條
  stocks/[symbol]/                # 個股頁：K 線圖 + 法人籌碼 + 新聞
    CandleChart.tsx               # lightweight-charts 多面板圖表（K線/量/外資/投信）
  settings/page.tsx               # 設定頁（訓練、回測、篩選條件）
  api/
    sync/route.ts                 # POST → SSE 串流，呼叫 ml/sync.py
    sync-status/route.ts          # GET → 各資料類型最後同步時間
    analyze/route.ts              # POST → 呼叫 rule_engine.py 或 predict.py
    recommendations/route.ts      # GET → 推薦清單（分頁 + 篩選 + 價格 fallback）
    stocks/[symbol]/route.ts      # GET → 個股價格 + 法人 + 財報
    stocks/[symbol]/news/route.ts # GET → 個股新聞
    feature-importance/route.ts   # GET → ML 特徵重要度
    train/route.ts                # POST → 觸發模型訓練
    backtest/route.ts             # POST → 觸發規則回測
lib/
  db/                             # better-sqlite3 + Drizzle ORM + migration
  analysis/ml-runner.ts           # child_process.spawn / stream 執行 Python
ml/                               # Python ML 模組（conda env: stock）
  sync.py                         # 資料同步：TWSE API + MOPS + yfinance → SQLite
  features.py                     # 特徵工程：19 維特徵
  fundamentals.py                 # 基本面計算（共用模組）
  strategies.py                   # Piotroski F-Score / PEG / Minervini SEPA
  rule_engine.py                  # 規則引擎：六層評分（基本面→估值→營收→籌碼→技術→大師共識）
  agents/                         # 投資大師 agent 模組（7 位）
    buffett.py / graham.py / munger.py / fisher.py
    druckenmiller.py / wood.py / ackman.py
  train.py                        # XGBoost 訓練（相對強勢標籤）
  predict.py                      # ML × 權重 + 規則 × 權重 混合預測
  backtest.py                     # 規則回測 → rule_scores.json
  stock_list.py                   # 上市股票清單
  sync_tags.py                    # AI 概念股標籤同步
  tests/                          # pytest 測試（41 tests）
nginx/stock.conf                  # nginx 設定（SSL + proxy → port 3031）
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
                  ml/sync.py
       (增量：全市場日API / 歷史：120 workers 並行)
       (籌碼：10天批次×2併發 / 指數退避重試)
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
ML 權重 = clamp((AUC - 0.50) / 0.20, 0, 0.80)
```

- ML 權重由模型交叉驗證 AUC 自動決定（AUC 越高 → ML 佔比越大）
  - AUC 0.55 → ML 25%、AUC 0.60 → ML 50%、AUC 0.65 → ML 75%
- AUC 計算：只取後 3 folds（早期 folds 訓練資料少會拖累）
- 規則引擎有一票否決權：規則判定 neutral → 最終一定是 neutral

### 動態門檻（大盤擇時）

```
buy_thresh   = 0.56 + (market_win_rate - 0.50) × 0.30
watch_thresh = 0.50 + (market_win_rate - 0.50) × 0.30
```

熊市（market_win_rate < 0.42）時自動提高門檻，減少誤推。

### 訊號判定邏輯（buy / watch / neutral）

| 條件 | 訊號 | 說明 |
|---|---|---|
| `final_score >= buy_thresh` | **買入** | 分數夠高、規則沒否決 |
| `watch_thresh ≤ final_score < buy_thresh` | **觀察** | 分數不夠買入，但有觀察價值 |
| `final_score < watch_thresh` | **中立** | 分數太低 |
| 規則 neutral 且無 reasons | **中立** | 硬性否決（TTM 虧損 / PE>60 / 近一年跌超 60% / Piotroski≤1 / 無基本面訊號） |
| 規則 neutral 但有 reasons | 最多 **觀察** | 風險扣分但基本面還行，ML 救不到買入 |

**「觀察」的實際意涵**：基本面還行（沒被前置過濾掉），但分數不夠好到推「買入」，通常是因為：
- 漲太多了（追高風險：近 20 日漲 > 15%）
- 量價背離（價漲量縮 → 不健康上漲）
- 技術面短期過熱（RSI > 80、乖離率 > 15%）
- 籌碼警告（外資/投信連續賣超 / 買賣超背離）

建議：放入追蹤名單，等拉回或訊號改善再考慮。

---

## 規則引擎（六層評分架構）

最終分數 = `底分 + 基本面加成 + 估值調整 + 營收加成 + 籌碼調整 + 技術微調 + 大師共識加成`

### 前置過濾（直接 neutral 退出）
- TTM 淨利為負 / PE > 60 / 近一年跌超 60% / Piotroski <= 1 / 無基本面訊號

### 第一層：基本面品質 → 底分 + 遞減加成
- ROE >= 20%、營收 YoY > 5%、獲利 YoY > 10%、負債 < 50%、Piotroski >= 4
- 超額勝率排序後遞減加權（100% → 40% → 20% → 10%）

### 第二層：估值調整（± 0.02 ~ 0.06）
- PE / PB / PEG / 殖利率 / 產業相對估值 / 追高風險懲罰

### 第三層：月營收加成（+ 0.01 ~ 0.05）
- 年增連 >= 6 月、加速成長、月增連 >= 3 月

### 第四層：籌碼調整（± 0.01 ~ 0.03）
- 外資/投信 60 日淨買（門檻 500 張）
- 外資/投信連續買賣超追蹤（≥ 3 日提醒、≥ 5 日加重）
- 10 日買賣超背離、融資融券

### 第五層：技術面微調（± 0.01 ~ 0.03）
- Minervini SEPA 趨勢、RS 相對強度、RSI、回調、量價背離

### 第六層：投資大師共識（± 0.05）
7 位大師各自以規則評估股票，平均權重加成到分數：
- **Buffett**：高 ROE + 低負債 + 合理 PE
- **Graham**：低 PE/PB + 殖利率（虧損直接否決）
- **Munger**：高 ROE + AI 產業護城河
- **Fisher**：高成長 + 月營收連續年增
- **Druckenmiller**：RS 排名 + Minervini 趨勢 + 短期動能
- **Wood**：AI 創新主題（GPU/機器人/雲端）+ 高成長
- **Ackman**：QARP（高 ROE + 穩成長 + 合理 PE）

每位回傳 bullish/neutral/bearish + confidence + reasons。5 位以上看多/看空會寫入推薦理由。

---

## XGBoost 模型

| 項目 | 值 |
|------|-----|
| 特徵數 | 19（技術 8 + 基本面 7 + 籌碼 2） |
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

---

## 資料來源

| 資料 | 來源 | 單位 | 更新頻率 |
|------|------|------|---------|
| 日 K 線 | TWSE 全市場日 API（增量）/ 個股月 API（歷史） | 張（股÷1000） | 每日 |
| 三大法人 | TWSE T86 API | 股（DB 原值） | 每日 |
| 融資融券 | TWSE MI_MARGN API | 張 | 每日 |
| 季度財報 | yfinance（主力）+ FinMind（補 EPS） | 元 | 每季 |
| 月營收 | MOPS 公開資訊觀測站 | 元 | 每月 |

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
- 同步資料（SSE 進度條 + ETA 預估）/ AI 分析（自動整合規則 + 大師共識 + ML 模型）
- 篩選：訊號（全部/買入/觀察/中立） + AI 主題下拉選單
- 表格排序：收盤價 / 漲跌幅 / 成交量 / 評分
- **大師共識欄位**：顯示 `X/7` 看多數（hover 看每位大師意見）
- 最後同步時間顯示

### 個股頁（`/stocks/[symbol]`）
- K 線圖：紅漲綠跌，OHLC + 量 + 外資 + 投信整合 tooltip，固定 bar 寬度
- 均線：MA5（黃）/ MA10（橙）/ MA20（青）/ MA60（紫），可切換
- 成交量 / 外資買賣超 / 投信買賣超柱狀圖（時間軸對齊）
- 初始顯示一年（250 交易日）
- 右側面板：季度財務 + 最新新聞
- Skeleton loading（載入中骨架屏）

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
# 本機開發
./dev.sh          # 啟動 Next.js dev server（port 3000）

# GCP 部署（不要加 sudo）
./deploy-gcp.sh   # build + 啟動 port 3031 + nginx SSL
```

### 測試

```bash
conda run -n stock python3 -m pytest ml/tests/ -v
```

41 個測試覆蓋：基本面計算、特徵工程、規則引擎、策略模組、資料完整性（價格-籌碼對齊）。

### 首次使用

1. **同步資料**（首次約 10–30 分鐘；增量同步用全市場 API 秒級完成）
2. **規則分析**（產生推薦清單）
3. 設定頁 → **訓練模型**（需 >= 200 筆有效資料）
4. **AI 分析**（ML + 規則混合推薦）

---

## 技術堆疊

| 層面 | 技術 |
|------|------|
| 前端 | Next.js 16（App Router）+ React 19 |
| 樣式 | Tailwind CSS v4（深色主題） |
| 圖表 | lightweight-charts 5.x（固定 bar 寬度、自訂滾動） |
| 資料庫 | SQLite（better-sqlite3）+ Drizzle ORM |
| ML | XGBoost 2.1 + scikit-learn + pandas-ta |
| 策略 | Piotroski F-Score + PEG Ratio + Minervini SEPA |
| 資料來源 | TWSE、yfinance、MOPS、FinMind |
| 並行 | ThreadPoolExecutor（歷史價格 120 workers、籌碼 10×2 workers、財報 20 workers） |
| 測試 | pytest（41 tests）+ Claude Code hook 自動觸發 |
| 部署 | GCP VM + nginx + Let's Encrypt SSL |
