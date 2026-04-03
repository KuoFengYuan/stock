import { NextRequest, NextResponse } from 'next/server'
import { runPythonScript } from '@/lib/analysis/ml-runner'
import fs from 'fs'
import path from 'path'

const RULE_SCORES_PATH = path.join(process.cwd(), 'ml', 'rule_scores.json')

export async function GET() {
  // 回傳現有的 rule_scores.json（若有）
  if (!fs.existsSync(RULE_SCORES_PATH)) {
    return NextResponse.json({ exists: false, data: null })
  }
  try {
    const data = JSON.parse(fs.readFileSync(RULE_SCORES_PATH, 'utf-8'))
    return NextResponse.json({ exists: true, data })
  } catch {
    return NextResponse.json({ exists: false, data: null })
  }
}

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}))
  const forwardDays = body.forwardDays ?? 60
  const minSamples = body.minSamples ?? 30

  const result = await runPythonScript('backtest.py', [
    '--forward-days', String(forwardDays),
    '--min-samples', String(minSamples),
  ])

  return NextResponse.json({
    success: result.success,
    output: result.output,
    error: result.error,
  })
}
