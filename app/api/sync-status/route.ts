import { NextResponse } from 'next/server'
import { getDb } from '@/lib/db'

export async function GET() {
  const db = getDb()

  const types = ['prices', 'financials', 'chips', 'monthly_revenue']
  const result: Record<string, { lastSync: number | null; records: number | null }> = {}

  for (const type of types) {
    const row = db.prepare(
      `SELECT finished_at, records_count FROM sync_log WHERE type = ? AND status = 'success' ORDER BY finished_at DESC LIMIT 1`
    ).get(type) as { finished_at: number; records_count: number } | undefined

    result[type] = {
      lastSync: row?.finished_at ?? null,
      records: row?.records_count ?? null,
    }
  }

  return NextResponse.json(result)
}
