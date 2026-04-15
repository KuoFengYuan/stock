import { NextResponse } from 'next/server'
import { runPythonScript } from '@/lib/analysis/ml-runner'
import { getDb } from '@/lib/db'

// 訓練腳本可能跑 1~5 分鐘，提高 API route timeout
export const maxDuration = 600

export async function POST() {
  const startedAt = Date.now()
  const result = await runPythonScript('train.py')
  const finishedAt = Date.now()

  // 寫入訓練紀錄，供 model-status 判斷是否需要重訓
  if (result.success) {
    const db = getDb()
    db.prepare(
      `INSERT INTO sync_log (type, status, records_count, started_at, finished_at) VALUES (?, ?, ?, ?, ?)`
    ).run('train', 'success', 0, startedAt, finishedAt)
  }

  return NextResponse.json({
    success: result.success,
    output: result.output,
    error: result.error,
  })
}
