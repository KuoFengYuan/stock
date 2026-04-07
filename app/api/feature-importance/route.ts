import { NextResponse } from 'next/server'
import { spawn } from 'child_process'
import path from 'path'

export async function GET() {
  const script = `
import pickle, json
from pathlib import Path
bundle = pickle.load(open(Path('ml/model.pkl'), 'rb'))
model = bundle['model']
feature_cols = bundle['feature_cols']
importances = model.feature_importances_.tolist()
result = {
  'auc': bundle.get('mean_auc', 0),
  'features': sorted(
    [{'name': f, 'importance': v} for f, v in zip(feature_cols, importances)],
    key=lambda x: -x['importance']
  )
}
print(json.dumps(result))
`

  return new Promise<NextResponse>((resolve) => {
    const fs = require('fs')
    const home = process.env.HOME || ''
    const condaCandidates = [
      process.env.CONDA_EXE,
      `${home}/miniconda3/bin/conda`,
      `${home}/miniconda3/condabin/conda`,
      '/usr/local/miniconda3/bin/conda',
      '/opt/homebrew/Caskroom/miniconda/base/bin/conda',
      'C:\\Users\\Will\\miniconda3\\Scripts\\conda.exe',
    ].filter(Boolean) as string[]

    const conda = condaCandidates.find(p => { try { return fs.existsSync(p) } catch { return false } })
      || 'conda'

    const cwd = path.join(process.cwd())
    const child = spawn(conda, ['run', '-n', 'stock', 'python3', '-c', script], { cwd })

    let out = ''
    let err = ''
    child.stdout.on('data', (d: Buffer) => { out += d.toString() })
    child.stderr.on('data', (d: Buffer) => { err += d.toString() })
    child.on('close', (code) => {
      if (code !== 0) {
        resolve(NextResponse.json({ error: err || 'failed' }, { status: 500 }))
        return
      }
      try {
        const data = JSON.parse(out.trim())
        resolve(NextResponse.json(data))
      } catch {
        resolve(NextResponse.json({ error: 'parse error', raw: out }, { status: 500 }))
      }
    })
  })
}
