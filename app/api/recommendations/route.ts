import { NextRequest, NextResponse } from 'next/server'
import { getDb } from '@/lib/db'
import { runMigrations } from '@/lib/db/migrate'

let migrated = false

function ensureDb() {
  if (!migrated) {
    runMigrations()
    migrated = true
  }
  return getDb()
}

export async function GET(req: NextRequest) {
  const db = ensureDb()
  const { searchParams } = new URL(req.url)

  // 取有 stock_prices 資料的最新推薦日期
  const latestRow = db.prepare(
    `SELECT r.date FROM recommendations r
     WHERE EXISTS (SELECT 1 FROM stock_prices p WHERE p.date = r.date LIMIT 1)
     ORDER BY r.date DESC LIMIT 1`
  ).get() as { date: string } | undefined
  const date = searchParams.get('date') || latestRow?.date || null
  const limit = parseInt(searchParams.get('limit') || '50')
  const offset = parseInt(searchParams.get('offset') || '0')

  if (!date) {
    return NextResponse.json({ date: null, total: 0, items: [] })
  }

  const filterRow = db.prepare('SELECT value FROM settings WHERE key = ?').get('filters') as { value: string } | undefined
  const filters = filterRow ? JSON.parse(filterRow.value) : {}

  const conditions: string[] = ['r.date = ?']
  const params: unknown[] = [date]

  if (filters.scoreMin != null) {
    conditions.push('r.score >= ?')
    params.push(filters.scoreMin)
  }
  if (filters.volumeMin != null) {
    conditions.push('(p.volume IS NULL OR p.volume >= ?)')
    params.push(filters.volumeMin)
  }
  if (filters.includeMarkets?.length > 0) {
    conditions.push(`s.market IN (${filters.includeMarkets.map(() => '?').join(',')})`)
    params.push(...filters.includeMarkets)
  }
  if (filters.excludeSymbols?.length > 0) {
    conditions.push(`r.symbol NOT IN (${filters.excludeSymbols.map(() => '?').join(',')})`)
    params.push(...filters.excludeSymbols)
  }

  const where = conditions.join(' AND ')

  const countRow = db.prepare(
    `SELECT COUNT(*) as total
     FROM recommendations r
     JOIN stocks s ON s.symbol = r.symbol
     LEFT JOIN stock_prices p ON p.symbol = r.symbol AND p.date = r.date
     WHERE ${where}`
  ).get(params) as { total: number }
  const total = countRow?.total || 0

  const rows = db.prepare(
    `SELECT r.symbol, s.name, s.market, r.score, r.signal,
            COALESCE(p.close, latest.close) as close,
            COALESCE(p.volume, latest.volume) as volume,
            r.reasons_json,
            prev.close as prev_close,
            prev.date  as prev_date
     FROM recommendations r
     JOIN stocks s ON s.symbol = r.symbol
     LEFT JOIN stock_prices p ON p.symbol = r.symbol AND p.date = r.date
     LEFT JOIN stock_prices latest ON latest.symbol = r.symbol
       AND latest.date = (
         SELECT date FROM stock_prices
         WHERE symbol = r.symbol
         ORDER BY date DESC LIMIT 1
       )
     LEFT JOIN stock_prices prev ON prev.symbol = r.symbol
       AND prev.date = (
         SELECT date FROM stock_prices
         WHERE symbol = r.symbol AND date < COALESCE(p.date, latest.date)
         ORDER BY date DESC LIMIT 1
       )
     WHERE ${where}
     ORDER BY r.score DESC
     LIMIT ? OFFSET ?`
  ).all([...params, limit, offset]) as {
    symbol: string; name: string; market: string; score: number; signal: string;
    close: number; prev_close: number; prev_date: string; volume: number; reasons_json: string;
  }[]

  // 預載所有 stock_tags（一次查詢）
  const tagRows = db.prepare(
    'SELECT symbol, tag, sub_tag FROM stock_tags'
  ).all() as { symbol: string; tag: string; sub_tag: string | null }[]
  const tagMap = new Map<string, { tag: string; sub_tag: string | null }[]>()
  for (const t of tagRows) {
    if (!tagMap.has(t.symbol)) tagMap.set(t.symbol, [])
    tagMap.get(t.symbol)!.push({ tag: t.tag, sub_tag: t.sub_tag })
  }

  const items = rows.map((row) => {
    // 前一交易日與當日相差超過 10 天 → 資料有缺口，不顯示漲跌幅
    const gapDays = row.prev_date && row.close
      ? (new Date(date!).getTime() - new Date(row.prev_date).getTime()) / 86400000
      : null
    const changePct = (row.prev_close && row.close && gapDays !== null && gapDays <= 10)
      ? ((row.close - row.prev_close) / row.prev_close) * 100
      : null

    return {
      symbol: row.symbol,
      name: row.name,
      market: row.market,
      score: row.score,
      signal: row.signal,
      close: row.close,
      changePct,
      volume: row.volume,
      reasons: row.reasons_json ? JSON.parse(row.reasons_json) : [],
      tags: tagMap.get(row.symbol) || [],
    }
  })

  return NextResponse.json({ date, total, items })
}
