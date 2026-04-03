import { NextRequest, NextResponse } from 'next/server'
import { getDb } from '@/lib/db'

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ symbol: string }> }
) {
  const { symbol } = await params
  const db = getDb()

  const stock = db.prepare(
    'SELECT symbol, name, market, industry FROM stocks WHERE symbol = ?'
  ).get(symbol) as { symbol: string; name: string; market: string; industry: string } | undefined

  if (!stock) return NextResponse.json({ error: 'not found' }, { status: 404 })

  const prices = db.prepare(
    `SELECT date, open, high, low, close, volume FROM stock_prices
     WHERE symbol = ?
     ORDER BY date DESC LIMIT 120`
  ).all(symbol).reverse() as { date: string; open: number; high: number; low: number; close: number; volume: number }[]

  const financials = db.prepare(
    'SELECT year, quarter, revenue, net_income, eps, equity, total_assets, total_debt FROM financials WHERE symbol = ? ORDER BY year DESC, quarter DESC LIMIT 8'
  ).all(symbol)

  const institutional = db.prepare(
    'SELECT date, foreign_net, trust_net, dealer_net, total_net FROM institutional WHERE symbol = ? ORDER BY date ASC'
  ).all(symbol) as { date: string; foreign_net: number; trust_net: number; dealer_net: number; total_net: number }[]

  return NextResponse.json({ ...stock, prices, financials, institutional })
}
