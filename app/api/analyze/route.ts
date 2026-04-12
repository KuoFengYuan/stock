import { NextRequest, NextResponse } from 'next/server'
import { runPythonScript } from '@/lib/analysis/ml-runner'
import path from 'path'
import fs from 'fs'

export async function POST(_req: NextRequest) {
  // 只保留一個分析入口：有模型就跑 ML（包含規則 + 大師共識 + ML 混合），
  // 沒模型就跑純規則 + 大師共識
  const modelPath = path.join(process.cwd(), 'ml', 'model.pkl')
  const hasModel = fs.existsSync(modelPath)
  const script = hasModel ? 'predict.py' : 'rule_engine.py'

  const result = await runPythonScript(script)

  return NextResponse.json({
    success: result.success,
    mode: hasModel ? 'ml' : 'rule',
    output: result.output,
    error: result.error,
  })
}
