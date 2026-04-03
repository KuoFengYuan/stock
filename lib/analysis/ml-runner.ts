import { spawn } from 'child_process'
import path from 'path'

export interface RunResult {
  success: boolean
  output: string
  error?: string
}

export function runPythonScript(scriptName: string, args: string[] = []): Promise<RunResult> {
  return new Promise((resolve) => {
    const scriptPath = path.join(process.cwd(), 'ml', scriptName)
    const proc = spawn('python', [scriptPath, ...args], {
      cwd: process.cwd(),
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    })

    let output = ''
    let errorOutput = ''

    proc.stdout.on('data', (data: Buffer) => { output += data.toString('utf-8') })
    proc.stderr.on('data', (data: Buffer) => { errorOutput += data.toString('utf-8') })

    proc.on('close', (code) => {
      if (code === 0) {
        resolve({ success: true, output })
      } else {
        resolve({ success: false, output, error: errorOutput || `exit code ${code}` })
      }
    })

    proc.on('error', (err) => {
      resolve({ success: false, output, error: err.message })
    })
  })
}
