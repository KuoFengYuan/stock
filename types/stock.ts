export type Market = 'TSE' | 'OTC'

export type Signal = 'buy' | 'watch' | 'neutral'

export interface Stock {
  symbol: string
  name: string
  market: Market
  industry?: string
}

export interface StockPrice {
  symbol: string
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  adjClose?: number
}

export interface Financials {
  symbol: string
  year: number
  quarter: number
  revenue?: number
  operatingProfit?: number
  netIncome?: number
  eps?: number
  equity?: number
  totalAssets?: number
  totalDebt?: number
}

export interface StockTag {
  tag: string
  sub_tag: string | null
}

export interface AgentDetail {
  name: string
  signal: 'bullish' | 'neutral' | 'bearish'
  confidence: number
  reasons: string[]
}

export interface AgentConsensus {
  bullish: number
  neutral: number
  bearish: number
}

export interface DimScores {
  fundamental: number
  momentum: number
  chip: number
  valuation: number
  consensus: number  // 0-7
}

export interface MlSubScores {
  main: number       // XGBRanker 全特徵排名
  breakout: number   // 動能/突破模型
  value: number      // 估值/基本面模型
  chip: number       // 籌碼/事件模型
  weights?: { main: number; breakout: number; value: number; chip: number }
}

export interface RecommendationItem {
  symbol: string
  name: string
  market: Market
  score: number
  signal: Signal
  close: number
  changePct: number | null
  volume: number
  reasons: string[]
  tags: StockTag[]
  agentConsensus?: AgentConsensus | null
  agentDetails?: AgentDetail[] | null
  dimScores?: DimScores | null
  mlSubScores?: MlSubScores | null
  // 基本面
  peRatio?: number
  pbRatio?: number
  pegRatio?: number | null
  niYoy?: number | null
  roe?: number
  // 技術面
  rsi14?: number
}

export interface FilterConfig {
  peRatioMax?: number
  pbRatioMax?: number
  roeMin?: number
  volumeMin?: number
  scoreMin?: number
  includeMarkets?: Market[]
  excludeSymbols?: string[]
}

export interface SyncStatus {
  type: string
  status: 'success' | 'error' | 'running'
  recordsCount?: number
  errorMessage?: string
  startedAt: number
  finishedAt?: number
}
