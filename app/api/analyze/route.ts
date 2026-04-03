import { NextRequest, NextResponse } from 'next/server'
import { runPythonScript } from '@/lib/analysis/ml-runner'
import path from 'path'
import fs from 'fs'

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}))
  const mode = body.mode || 'rule' // "rule" | "ml"

  const modelPath = path.join(process.cwd(), 'ml', 'model.pkl')
  const hasModel = fs.existsSync(modelPath)

  let script: string
  if (mode === 'ml' && hasModel) {
    script = 'predict.py'
  } else if (mode === 'ml' && !hasModel) {
    return NextResponse.json({
      success: false,
      error: '尚未訓練模型，請先至「設定」頁面執行「重新訓練模型」',
    })
  } else {
    script = 'rule_engine.py'
  }

  const result = await runPythonScript(script)

  return NextResponse.json({
    success: result.success,
    mode: mode === 'ml' && hasModel ? 'ml' : mode === 'ml' ? 'ml_trained' : 'rule',
    output: result.output,
    error: result.error,
  })
}
