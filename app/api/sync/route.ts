import { NextRequest } from 'next/server'
import { streamPythonScript } from '@/lib/analysis/ml-runner'
import { getDb } from '@/lib/db'

const FINANCIALS_MIN_INTERVAL_MS = 7 * 24 * 60 * 60 * 1000

function getLastFinancialsSync(): number | null {
  const db = getDb()
  const row = db.prepare(
    `SELECT finished_at FROM sync_log WHERE type = 'financials' AND status = 'success' ORDER BY finished_at DESC LIMIT 1`
  ).get() as { finished_at: number } | undefined
  return row?.finished_at ?? null
}

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}))
  let mode = body.mode || 'all'
  const force = body.force === true

  let skippedFinancials = false

  if ((mode === 'financials' || mode === 'all') && !force) {
    const lastSync = getLastFinancialsSync()
    if (lastSync && Date.now() - lastSync < FINANCIALS_MIN_INTERVAL_MS) {
      const daysSince = Math.floor((Date.now() - lastSync) / (1000 * 60 * 60 * 24))
      if (mode === 'financials') {
        const msg = JSON.stringify({
          type: 'done',
          success: true,
          skippedFinancials: true,
          output: `季報在 ${daysSince} 天前已同步，略過（距下次可同步還有 ${7 - daysSince} 天）。傳入 force:true 可強制執行。`,
        })
        return new Response(msg + '\n', { headers: { 'Content-Type': 'text/plain; charset=utf-8' } })
      }
      mode = 'prices_chips'
      skippedFinancials = true
    }
  }

  const encoder = new TextEncoder()

  const stream = new ReadableStream({
    async start(controller) {
      const send = (obj: object) => {
        controller.enqueue(encoder.encode(JSON.stringify(obj) + '\n'))
      }

      if (skippedFinancials) {
        send({ type: 'line', text: '（季報近期已同步，本次僅更新價格）' })
      }

      // 串流 sync.py
      let syncSuccess = true
      let syncOutput = ''
      await streamPythonScript('sync.py', [mode], (line) => {
        syncOutput += line + '\n'
        send({ type: 'line', text: line })
      }).then((result) => {
        syncSuccess = result.success
        if (!result.success && result.error) {
          send({ type: 'line', text: `[ERROR] ${result.error}` })
        }
      })

      send({ type: 'done', success: syncSuccess, skippedFinancials })
      controller.close()
    },
  })

  return new Response(stream, {
    headers: {
      'Content-Type': 'text/plain; charset=utf-8',
      'X-Content-Type-Options': 'nosniff',
    },
  })
}
