@AGENTS.md

# Rules

## 溝通風格
- 用繁體中文回覆，code/commit 用英文
- 直接做，不要問「要不要我…」。做完再簡短回報結果
- 不要重述我說過的話、不要解釋你要做什麼，直接開始
- 回覆盡量簡短，省略不必要的過渡句和總結段落

## 寫 Code 原則
- 改動前先讀檔，理解現有邏輯再動手
- 改最少的 code 達成目標，不要順手重構、加註解、改命名
- 不要加 try-catch / 錯誤處理 / fallback 除非我要求
- 不要加 console.log / print debug 除非在排查問題
- 一個任務改完就停，不要自作主張做額外的「改善」
- Python 用 `conda run -n stock python3`，不要用裸 python3
- 前端台股色彩：紅漲(#ef4444) 綠跌(#22c55e)，跟美股相反

## Git
- commit message 用英文，簡潔描述 why 不是 what
- 不要 amend，不要 force push，不要改 git config
- push 前不需要確認，我說 push 就直接 push

## 測試
- 測試自動執行：每次 Edit/Write 會觸發 `pytest ml/tests/ -x -q`（PostToolUse hook）
- 手動跑：`conda run -n stock python3 -m pytest ml/tests/ -v`
- 測試覆蓋：基本面(fundamentals)、特徵(features)、規則引擎(rule_engine)、策略(strategies)、資料完整性(data_integrity)
- 新增功能時要補對應測試，改動邏輯要確保現有測試通過
- 測試失敗時停下來修，不要跳過

## 除錯
- 先讀 error message，定位根因，不要盲猜亂試
- 改一處測一次，不要一次改多處
- DB 相關問題先用 SQL 查資料確認，不要只看 code 推測

# 台股 AI 選股系統

## 專案概覽

Next.js App Router 前端 + Python ML 後端的台股分析系統。SQLite 儲存，conda env `stock` 執行 Python。

## 目錄結構

```
app/                    # Next.js App Router 前端
  page.tsx              # 首頁：推薦清單 + 同步/分析按鈕 + 排序 + AI主題篩選
  stocks/[symbol]/      # 個股頁：K 線圖 + 整合 tooltip（OHLC/量/外資/投信）
  api/                  # API routes
    sync/               # POST → SSE 串流，呼叫 ml/sync.py
    sync-status/        # GET → 各資料類型最後同步時間
    analyze/            # POST → 有 model.pkl 就跑 predict.py（ML+規則+大師），否則 rule_engine.py
    recommendations/    # GET → 查詢 recommendations 表（含價格 fallback）
    stocks/             # GET → 個股價格 + 法人資料
    train/              # POST → 呼叫 train.py
    backtest/           # POST → 呼叫 backtest.py
lib/
  db/                   # better-sqlite3 連線 + Drizzle schema + migration
  analysis/ml-runner.ts # 執行 Python 腳本（spawn / stream）
ml/                     # Python ML 模組（conda env: stock）
  sync.py               # 資料同步：TWSE/TPEX API → SQLite
  features.py           # 特徵工程：19 維特徵（技術/基本面/籌碼）
  fundamentals.py       # 基本面計算（共用模組）
  strategies.py         # Piotroski / PEG / Minervini 策略
  rule_engine.py        # 規則引擎：六層評分（基本面→估值→營收→籌碼→技術→大師共識）
  agents/               # 投資大師 agent 模組（Buffett/Graham/Munger/Fisher/Druckenmiller/Wood/Ackman）
  train.py              # XGBoost 訓練：相對強勢標籤（top 30%）
  predict.py            # 混合預測：ML × 權重 + 規則 × 權重
  backtest.py           # 規則回測 → rule_scores.json
  stock_list.py         # 股票清單（僅上市 .TW）
  tests/                # pytest 測試
data/stock.db           # SQLite 資料庫（僅上市）
```

## 核心架構

### 雙軌混合評分
```
最終分數 = ML分數 × ML權重 + 規則分數 × 規則權重
ML 權重 = clamp((AUC - 0.50) / 0.20 × 0.80, 0, 0.80)
```
規則引擎有一票否決權：規則 neutral → 不論 ML 多高都壓到 neutral。

### 規則引擎六層結構 (rule_engine.py)
1. **基本面品質**：底分 + 遞減加成（ROE/營收/獲利/負債/Piotroski）
2. **估值乘數**：PE/PB/PEG/殖利率/產業相對/追高懲罰（×0.70~1.15）
3. **月營收乘數**：年增連月/加速成長（×1.00~1.15）
4. **籌碼乘數**：外資投信 60d 淨買、10d 買賣超背離、融資融券（×0.75~1.15）
5. **技術面微調**：Minervini 趨勢/RS 強度/RSI/回調/量價背離（±0.06）
6. **大師共識**：7 位投資大師（Buffett/Graham/Munger/Fisher/Druckenmiller/Wood/Ackman）平均權重軟加分（±0.05）
   - 規則式實作（非 LLM），每位接收 fund/tech/monthly/tags，回傳 bullish/neutral/bearish
   - `apply_agents()` 同時被 rule_engine.py 和 predict.py 呼叫，確保規則分析和 AI 分析都有共識

### 動態門檻（大盤擇時）
```
buy_thresh   = 0.56 + (market_win_rate - 0.50) × 0.30
watch_thresh = 0.50 + (market_win_rate - 0.50) × 0.30
熊市 (win_rate < 0.42) → buy ≥ 0.58, watch ≥ 0.52
```

### 前置過濾（直接 neutral）
- 近一年跌超 60%、TTM 淨利為負、PE > 60、Piotroski ≤ 1、無基本面訊號

## 資料來源與單位

| 資料 | 來源 | 單位 |
|---|---|---|
| 日 K 線 | TWSE 全市場日 API（增量）/ 個股月 API（歷史） | 張（股÷1000）|
| 三大法人 | TWSE T86 / TPEX 3itrade | **股**（DB 原值） |
| 融資融券 | TWSE MI_MARGN | 張 |
| 財報 | yfinance | 元 |
| 月營收 | MOPS 公開資訊觀測站 | 元 |

**重要**：`institutional` 表存的是「股」，顯示時 `÷1000` 換算「張」。籌碼門檻 `CHIP_MIN_ABS = 500_000`（股）= 500 張。

## DB Schema 重點

- `stocks`: symbol(PK), name, market(TSE/OTC)
- `stock_prices`: symbol, date, OHLCV（UNIQUE symbol+date）
- `institutional`: symbol, date, foreign_net/trust_net/dealer_net/total_net（單位：股）
- `margin_trading`: symbol, date, margin/short balance
- `financials`: symbol, year, quarter, revenue/eps/equity/...
- `monthly_revenue`: symbol, year, month, revenue/yoy/mom
- `recommendations`: symbol, date, score, signal(buy/watch/neutral), reasons_json
- `sync_log`: type, status, records_count, timestamps

## 開發注意事項

- Python 環境：`conda run -n stock python3 ml/xxx.py`
- 前端漲跌色：**紅漲綠跌**（台股慣例，與美股相反）
- K 線圖：lightweight-charts，upColor=#ef4444(紅), downColor=#22c55e(綠)，初始顯示 250 bars（一年）
- 圖表 tooltip：OHLC + 量 + 外資 + 投信整合在 K 線 crosshair，子圖表不顯示 tooltip
- sync_prices 增量用全市場日 API（一天一 request），歷史用個股月 API（120 workers）
- sync_chips 用交易日過濾（跳過假日）+ 指數退避重試（3 次）+ 10 天批次 ×2 併發
- EPS 回填：yfinance Q4 缺 EPS 時用同股其他季 EPS/淨利比例推算
- 特徵填補：預測時用訓練集 median（存在 model.pkl bundle 裡）
- 基本面 lookahead bias 防護：用保守公告日（Q1→5/15, Q2→8/14, Q3→11/14, Q4→次年4/30）
- ROE 用平均 equity（當季+前季）、股數用 4 季中位數
- pandas 3.0：groupby 標籤用 `transform` 而非 `apply`（apply 會移除 key 欄）
- recommendations API 的 limit 預設 50，前端用 `limit=2000` 載入全部
- 只處理上市股票（.TW / TSE），不含上櫃（.TWO / OTC）：sync/features/predict/backtest 全部只查 market='TSE'
