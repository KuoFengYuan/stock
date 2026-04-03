import { NextRequest, NextResponse } from 'next/server'
import { getDb } from '@/lib/db'

export async function GET() {
  const db = getDb()
  const row = db.prepare('SELECT value FROM settings WHERE key = ?').get('filters') as { value: string } | undefined
  return NextResponse.json(row ? JSON.parse(row.value) : {})
}

export async function POST(req: NextRequest) {
  const body = await req.json()
  const db = getDb()
  db.prepare('INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?,?,?)').run(
    'filters', JSON.stringify(body), Date.now()
  )
  return NextResponse.json({ ok: true })
}
