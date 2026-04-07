import { spawn } from 'child_process'
import path from 'path'
import fs from 'fs'

function getPython(): string {
  const home = process.env.HOME || ''
  const candidates = [
    `${home}/miniconda3/envs/stock/bin/python`,
    '/usr/local/miniconda3/envs/stock/bin/python',
    'python3',
    'python',
  ]
  return candidates.find(p => { try { return p.startsWith('/') && fs.existsSync(p) } catch { return false } })
    ?? 'python'
}

export interface RunResult {
  success: boolean
  output: string
  error?: string
}

export function runPythonScript(scriptName: string, args: string[] = []): Promise<RunResult> {
  return new Promise((resolve) => {
    const scriptPath = path.join(process.cwd(), 'ml', scriptName)
    const proc = spawn(getPython(), [scriptPath, ...args], {
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

/**
 * 串流執行 Python 腳本，每行 stdout 即時透過 onLine 回呼推送。
 * 回傳 Promise<RunResult>（腳本結束後 resolve）。
 */
export function streamPythonScript(
  scriptName: string,
  args: string[],
  onLine: (line: string) => void,
): Promise<RunResult> {
  return new Promise((resolve) => {
    const scriptPath = path.join(process.cwd(), 'ml', scriptName)
    const proc = spawn(getPython(), [scriptPath, ...args], {
      cwd: process.cwd(),
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    })

    let output = ''
    let errorOutput = ''
    let buf = ''

    proc.stdout.on('data', (data: Buffer) => {
      const chunk = data.toString('utf-8')
      output += chunk
      buf += chunk
      const lines = buf.split('\n')
      buf = lines.pop() ?? ''
      for (const line of lines) {
        onLine(line)
      }
    })

    proc.stderr.on('data', (data: Buffer) => { errorOutput += data.toString('utf-8') })

    proc.on('close', (code) => {
      if (buf) onLine(buf)
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
