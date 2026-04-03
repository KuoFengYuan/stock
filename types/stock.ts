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
  // 基本面
  peRatio?: number
  pbRatio?: number
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
