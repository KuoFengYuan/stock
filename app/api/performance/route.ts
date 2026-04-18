import { NextRequest, NextResponse } from 'next/server'
import { getDb } from '@/lib/db'
import { runMigrations } from '@/lib/db/migrate'

let migrated = false
function ensureDb() {
  if (!migrated) { runMigrations(); migrated = true }
  return getDb()
}

// 台股一買一賣交易成本：手續費 0.1425% × 2 + 證交稅 0.3%
const TX_COST = 0.585

// 推薦追蹤：報酬 + 相對大盤超額 + 扣成本淨利（5d / 20d 為主）
export async function GET(req: NextRequest) {
  const db = ensureDb()
  const { searchParams } = new URL(req.url)
  const days = Math.min(Math.max(parseInt(searchParams.get('days') || '90'), 7), 365)
  const cutoff = new Date(Date.now() - days * 86400000).toISOString().slice(0, 10)

  const recs = db.prepare(`
    SELECT symbol, date, signal, features_json
    FROM recommendations
    WHERE date >= ? AND signal IN ('buy','watch','neutral')
    ORDER BY symbol, date
  `).all(cutoff) as { symbol: string; date: string; signal: string; features_json: string | null }[]

  if (recs.length === 0) {
    return NextResponse.json({ days, total: 0, by_signal: {}, by_model: {}, note: 'no data' })
  }

  // 各股票 date->close 時序
  const symbols = [...new Set(recs.map(r => r.symbol))]
  const priceMap = new Map<string, { date: string; close: number }[]>()
  const q = db.prepare('SELECT date, close FROM stock_prices WHERE symbol = ? ORDER BY date')
  for (const s of symbols) {
    priceMap.set(s, q.all(s) as { date: string; close: number }[])
  }

  // 大盤基準：用當日全市場股票收盤平均 return（等權指數代理）
  // 算法：每檔股票在每個交易日的 N 日 forward ret，group by date 取 mean
  const allDates = new Set(recs.map(r => r.date))
  const mktCache = new Map<string, { m5: number | null; m20: number | null }>()

  function fwdRet(symbol: string, date: string, step: number): number | null {
    const prices = priceMap.get(symbol)
    if (!prices) return null
    const idx = prices.findIndex(p => p.date === date)
    if (idx < 0 || idx + step >= prices.length) return null
    const p0 = prices[idx].close
    const pN = prices[idx + step].close
    if (!p0 || p0 <= 0) return null
    return (pN - p0) / p0 * 100
  }

  // 計算大盤基準：取該日所有推薦股票的 mean forward ret
  for (const d of allDates) {
    const sameDay = recs.filter(r => r.date === d)
    let sum5 = 0, n5 = 0, sum20 = 0, n20 = 0
    for (const r of sameDay) {
      const r5 = fwdRet(r.symbol, r.date, 5)
      const r20 = fwdRet(r.symbol, r.date, 20)
      if (r5 != null) { sum5 += r5; n5++ }
      if (r20 != null) { sum20 += r20; n20++ }
    }
    mktCache.set(d, {
      m5: n5 > 0 ? sum5 / n5 : null,
      m20: n20 > 0 ? sum20 / n20 : null,
    })
  }

  type Bucket = {
    // 5 日
    n5: number; sum5: number; sumExcess5: number; hitBeat5: number;
    // 20 日
    n20: number; sum20: number; sumExcess20: number; hitBeat20: number;
  }
  const mk = (): Bucket => ({
    n5: 0, sum5: 0, sumExcess5: 0, hitBeat5: 0,
    n20: 0, sum20: 0, sumExcess20: 0, hitBeat20: 0,
  })

  const bySignal: Record<string, Bucket> = { buy: mk(), watch: mk(), neutral: mk() }
  const byModel: Record<string, Bucket> = { main: mk(), breakout: mk(), value: mk(), chip: mk() }

  function addRet(b: Bucket, r5: number | null, r20: number | null, mkt5: number | null, mkt20: number | null) {
    if (r5 != null && mkt5 != null) {
      b.n5++
      b.sum5 += r5
      b.sumExcess5 += (r5 - mkt5)
      if (r5 > mkt5) b.hitBeat5++
    }
    if (r20 != null && mkt20 != null) {
      b.n20++
      b.sum20 += r20
      b.sumExcess20 += (r20 - mkt20)
      if (r20 > mkt20) b.hitBeat20++
    }
  }

  // By signal
  for (const r of recs) {
    const b = bySignal[r.signal]
    if (!b) continue
    const mkt = mktCache.get(r.date) || { m5: null, m20: null }
    addRet(b,
      fwdRet(r.symbol, r.date, 5),
      fwdRet(r.symbol, r.date, 20),
      mkt.m5, mkt.m20)
  }

  // By sub-model：每天該模型 Top 20 模擬組合
  const byDate = new Map<string, typeof recs>()
  for (const r of recs) {
    if (!byDate.has(r.date)) byDate.set(r.date, [])
    byDate.get(r.date)!.push(r)
  }
  for (const [date, items] of byDate) {
    const withSubs = items.map(i => {
      try {
        const f = i.features_json ? JSON.parse(i.features_json) : null
        return { ...i, subs: f?.ml_sub_scores ?? null }
      } catch { return { ...i, subs: null } }
    }).filter(i => i.subs)
    const mkt = mktCache.get(date) || { m5: null, m20: null }

    for (const m of ['main', 'breakout', 'value', 'chip'] as const) {
      const ranked = [...withSubs].sort((a, b) => (b.subs![m] ?? 0) - (a.subs![m] ?? 0)).slice(0, 20)
      for (const r of ranked) {
        addRet(byModel[m],
          fwdRet(r.symbol, r.date, 5),
          fwdRet(r.symbol, r.date, 20),
          mkt.m5, mkt.m20)
      }
    }
  }

  function summarize(b: Bucket) {
    const avg5 = b.n5 > 0 ? b.sum5 / b.n5 : null
    const avg20 = b.n20 > 0 ? b.sum20 / b.n20 : null
    return {
      n5: b.n5, n20: b.n20,
      avg5, avg20,                                                  // 原始平均報酬
      excess5: b.n5 > 0 ? b.sumExcess5 / b.n5 : null,              // vs 大盤超額
      excess20: b.n20 > 0 ? b.sumExcess20 / b.n20 : null,
      net5: avg5 != null ? avg5 - TX_COST : null,                  // 扣交易成本
      net20: avg20 != null ? avg20 - TX_COST : null,
      beat5: b.n5 > 0 ? b.hitBeat5 / b.n5 : null,                  // 勝過大盤比例
      beat20: b.n20 > 0 ? b.hitBeat20 / b.n20 : null,
    }
  }

  return NextResponse.json({
    days,
    total: recs.length,
    tx_cost: TX_COST,
    by_signal: Object.fromEntries(Object.entries(bySignal).map(([k, v]) => [k, summarize(v)])),
    by_model: Object.fromEntries(Object.entries(byModel).map(([k, v]) => [k, summarize(v)])),
  })
}
