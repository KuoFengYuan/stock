import { sqliteTable, text, integer, real, uniqueIndex, index } from 'drizzle-orm/sqlite-core'

export const stocks = sqliteTable('stocks', {
  symbol: text('symbol').primaryKey(),
  name: text('name').notNull(),
  market: text('market').notNull(), // "TSE" | "OTC"
  industry: text('industry'),
  listedDate: text('listed_date'),
  updatedAt: integer('updated_at').notNull(),
})

export const stockPrices = sqliteTable('stock_prices', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  symbol: text('symbol').notNull().references(() => stocks.symbol),
  date: text('date').notNull(),
  open: real('open').notNull(),
  high: real('high').notNull(),
  low: real('low').notNull(),
  close: real('close').notNull(),
  volume: integer('volume').notNull(),
  adjClose: real('adj_close'),
}, (t) => [
  uniqueIndex('idx_prices_symbol_date').on(t.symbol, t.date),
  index('idx_prices_date').on(t.date),
])

export const financials = sqliteTable('financials', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  symbol: text('symbol').notNull(),
  year: integer('year').notNull(),
  quarter: integer('quarter').notNull(),
  revenue: real('revenue'),
  operatingProfit: real('operating_profit'),
  netIncome: real('net_income'),
  eps: real('eps'),
  equity: real('equity'),
  totalAssets: real('total_assets'),
  totalDebt: real('total_debt'),
}, (t) => [
  uniqueIndex('idx_financials_symbol_year_quarter').on(t.symbol, t.year, t.quarter),
])

export const recommendations = sqliteTable('recommendations', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  symbol: text('symbol').notNull(),
  date: text('date').notNull(),
  score: real('score').notNull(),
  signal: text('signal').notNull(), // "buy" | "watch" | "neutral"
  featuresJson: text('features_json'),
  reasonsJson: text('reasons_json'),
  modelVersion: text('model_version'),
  createdAt: integer('created_at').notNull(),
}, (t) => [
  uniqueIndex('idx_rec_symbol_date').on(t.symbol, t.date),
  index('idx_rec_date_score').on(t.date, t.score),
])

export const settings = sqliteTable('settings', {
  key: text('key').primaryKey(),
  value: text('value').notNull(),
  updatedAt: integer('updated_at').notNull(),
})

export const syncLog = sqliteTable('sync_log', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  type: text('type').notNull(), // "prices" | "financials" | "analysis"
  status: text('status').notNull(), // "success" | "error" | "running"
  recordsCount: integer('records_count'),
  errorMessage: text('error_message'),
  startedAt: integer('started_at').notNull(),
  finishedAt: integer('finished_at'),
})
