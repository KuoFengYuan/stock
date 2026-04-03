import { NextRequest, NextResponse } from 'next/server'
import { runPythonScript } from '@/lib/analysis/ml-runner'
import { getDb } from '@/lib/db'

// 季報最短重新同步間隔（毫秒）：7 天
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
  let mode = body.mode || 'all' // "prices" | "financials" | "all"
  const force = body.force === true // 強制重新同步季報

  let skippedFinancials = false

  // 若 mode 包含 financials，檢查是否在間隔內
  if ((mode === 'financials' || mode === 'all') && !force) {
    const lastSync = getLastFinancialsSync()
    if (lastSync && Date.now() - lastSync < FINANCIALS_MIN_INTERVAL_MS) {
      const daysSince = Math.floor((Date.now() - lastSync) / (1000 * 60 * 60 * 24))
      if (mode === 'financials') {
        return NextResponse.json({
          success: true,
          output: `季報在 ${daysSince} 天前已同步，略過（距下次可同步還有 ${7 - daysSince} 天）。傳入 force:true 可強制執行。`,
          skippedFinancials: true,
        })
      }
      // mode === 'all'：降級為只同步價格
      mode = 'prices'
      skippedFinancials = true
    }
  }

  const syncResult = await runPythonScript('sync.py', [mode])
  let output = syncResult.output
  if (skippedFinancials) {
    output = '（季報近期已同步，本次僅更新價格）\n' + output
  }

  // 同步完後自動跑規則分析，更新推薦
  if (syncResult.success) {
    const analyzeResult = await runPythonScript('rule_engine.py')
    output += '\n--- 規則分析 ---\n' + analyzeResult.output
  }

  return NextResponse.json({
    success: syncResult.success,
    output,
    error: syncResult.error,
    skippedFinancials,
  })
}
