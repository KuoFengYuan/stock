'use client'
import { useState, useEffect, useRef } from 'react'
import type { FilterConfig } from '@/types/stock'

function useTimer(running: boolean) {
  const [elapsed, setElapsed] = useState(0)
  const startRef = useRef<number | null>(null)
  useEffect(() => {
    if (running) {
      startRef.current = Date.now()
      setElapsed(0)
      const id = setInterval(() => setElapsed(Math.floor((Date.now() - startRef.current!) / 1000)), 1000)
      return () => clearInterval(id)
    }
  }, [running])
  return elapsed
}

interface ModelStatus {
  modelExists: boolean
  modelMtime: number | null
  lastTrainAt: number | null
  newPricesSinceTrain: number
  shouldRetrain: boolean
  retrainThreshold: number
  priceCount: number
  symbolCount: number
}

interface BacktestData {
  generated_at: string
  forward_days: number
  market_abs_win_rate?: number
  rules: Record<string, {
    win_rate: number | null
    excess_win_rate?: number | null
    avg_excess_return_pct: number | null
    sample_count: number
    score: number
    status: string
    market_abs_win_rate?: number
  }>
}

export default function SettingsPage() {
  const [filters, setFilters] = useState<FilterConfig>({})
  const [saved, setSaved] = useState(false)

  // 模型訓練
  const [training, setTraining] = useState(false)
  const [trainLog, setTrainLog] = useState('')
  const [trainDone, setTrainDone] = useState<string | null>(null)
  const trainElapsed = useTimer(training)

  // 模型狀態
  const [modelStatus, setModelStatus] = useState<ModelStatus | null>(null)

  // 規則回測
  const [backtesting, setBacktesting] = useState(false)
  const [backtestLog, setBacktestLog] = useState('')
  const [backtestDone, setBacktestDone] = useState<string | null>(null)
  const [backtestData, setBacktestData] = useState<BacktestData | null>(null)
  const backtestElapsed = useTimer(backtesting)

  useEffect(() => {
    fetch('/api/filters').then(r => r.json()).then(setFilters)
    fetchModelStatus()
    fetchBacktestData()
  }, [])

  function fetchModelStatus() {
    fetch('/api/model-status').then(r => r.json()).then(setModelStatus)
  }

  function fetchBacktestData() {
    fetch('/api/backtest').then(r => r.json()).then(d => {
      if (d.exists) setBacktestData(d.data)
    })
  }

  async function handleSave() {
    await fetch('/api/filters', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(filters),
    })
    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
  }

  async function handleTrain() {
    setTraining(true)
    setTrainLog('')
    setTrainDone(null)
    const start = Date.now()
    try {
      const res = await fetch('/api/train', { method: 'POST' })
      const json = await res.json()
      setTrainLog(json.output || json.error || '')
      const took = Math.floor((Date.now() - start) / 1000)
      setTrainDone(json.success ? `訓練完成（耗時 ${took} 秒）` : `訓練失敗（${took} 秒）`)
    } finally {
      setTraining(false)
      fetchModelStatus()
    }
  }

  async function handleBacktest() {
    setBacktesting(true)
    setBacktestLog('')
    setBacktestDone(null)
    const start = Date.now()
    try {
      const res = await fetch('/api/backtest', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) })
      const json = await res.json()
      setBacktestLog(json.output || json.error || '')
      const took = Math.floor((Date.now() - start) / 1000)
      setBacktestDone(json.success ? `回測完成（耗時 ${took} 秒）` : `回測失敗（${took} 秒）`)
      if (json.success) fetchBacktestData()
    } finally {
      setBacktesting(false)
    }
  }

  function update(key: keyof FilterConfig, value: unknown) {
    setFilters(prev => ({ ...prev, [key]: value === '' ? undefined : value }))
  }

  return (
    <div className="max-w-lg">
      <h1 className="text-2xl font-bold text-white mb-6">設定</h1>

      {/* ── 模型狀態與重訓提醒 ── */}
      <section className="mb-8 p-4 bg-slate-800 rounded-xl">
        <h2 className="text-slate-200 font-medium mb-3">AI 模型訓練</h2>
        {modelStatus && (
          <div className="mb-4 space-y-1.5 text-xs">
            <div className="flex justify-between">
              <span className="text-slate-400">模型狀態</span>
              <span className={modelStatus.modelExists ? 'text-green-400' : 'text-red-400'}>
                {modelStatus.modelExists ? '已存在' : '尚未訓練'}
              </span>
            </div>
            {modelStatus.lastTrainAt && (
              <div className="flex justify-between">
                <span className="text-slate-400">上次訓練</span>
                <span className="text-slate-300">{new Date(modelStatus.lastTrainAt).toLocaleDateString('zh-TW')}</span>
              </div>
            )}
            <div className="flex justify-between">
              <span className="text-slate-400">資料量</span>
              <span className="text-slate-300">{modelStatus.symbolCount} 檔 / {modelStatus.priceCount.toLocaleString()} 筆</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">上次訓練後新增</span>
              <span className={modelStatus.shouldRetrain ? 'text-amber-400 font-medium' : 'text-slate-300'}>
                {modelStatus.newPricesSinceTrain.toLocaleString()} 筆
              </span>
            </div>
            {modelStatus.shouldRetrain && (
              <div className="mt-2 px-3 py-2 bg-amber-900/40 border border-amber-700/50 rounded-lg text-amber-300 text-xs">
                資料量已增加 {modelStatus.newPricesSinceTrain.toLocaleString()} 筆（門檻 {modelStatus.retrainThreshold.toLocaleString()}），建議重新訓練模型。
              </div>
            )}
          </div>
        )}
        <p className="text-slate-400 text-xs mb-4">
          使用資料庫中所有歷史價格與財務資料重新訓練 XGBoost 模型。約需 1～3 分鐘。
        </p>
        <button
          onClick={handleTrain}
          disabled={training}
          className="px-5 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm rounded-lg transition-colors"
        >
          {training ? `訓練中… ${trainElapsed} 秒` : '重新訓練模型'}
        </button>
        {trainDone && (
          <span className={`ml-3 text-sm ${trainDone.includes('完成') ? 'text-green-400' : 'text-red-400'}`}>
            {trainDone}
          </span>
        )}
        {trainLog && (
          <pre className="mt-3 p-3 bg-slate-900 text-slate-300 text-xs rounded-lg overflow-auto max-h-48 whitespace-pre-wrap">
            {trainLog.split('\n').filter(l => !/[^\x00-\x7F\u4e00-\u9fff\u3400-\u4dbf\uff00-\uffef\s]/.test(l)).join('\n')}
          </pre>
        )}
      </section>

      {/* ── 規則回測 ── */}
      <section className="mb-8 p-4 bg-slate-800 rounded-xl">
        <h2 className="text-slate-200 font-medium mb-1">規則回測校準</h2>
        <p className="text-slate-400 text-xs mb-4">
          對歷史資料計算每條規則的實際勝率，自動更新規則分數。<br />
          建議資料累積半年以上後執行，約需 3～10 分鐘。
        </p>
        {backtestData && (
          <div className="mb-4">
            <div className="text-xs text-slate-500 mb-2">
              上次回測：{new Date(backtestData.generated_at).toLocaleDateString('zh-TW')}｜預測窗口：{backtestData.forward_days} 天
              {backtestData.market_abs_win_rate != null && (
                <span className="ml-2 text-slate-400">｜市場基準勝率：{(backtestData.market_abs_win_rate * 100).toFixed(1)}%</span>
              )}
            </div>
            <div className="overflow-auto max-h-72 rounded-lg border border-slate-700">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-slate-700/50 text-slate-400 whitespace-nowrap">
                    <th className="text-left px-3 py-2">規則</th>
                    <th className="text-left px-3 py-2">說明</th>
                    <th className="text-right px-3 py-2">樣本數</th>
                    <th className="text-right px-3 py-2">勝率</th>
                    <th className="text-right px-3 py-2">超額報酬</th>
                    <th className="text-right px-3 py-2">分數</th>
                    <th className="text-right px-3 py-2">狀態</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(backtestData.rules).map(([rule, s]) => {
                    const baseline = backtestData.market_abs_win_rate ?? 0.45
                    const isAboveBaseline = s.win_rate !== null && s.win_rate >= baseline
                    const isMuchAbove = s.win_rate !== null && s.win_rate >= baseline + 0.05
                    const isBelow = s.win_rate !== null && s.win_rate < baseline - 0.02
                    const suppressed = s.win_rate !== null && s.win_rate < baseline
                    const RULE_LABELS: Record<string, string> = {
                      rsi_oversold:     'RSI < 30，極度超賣',
                      rsi_low:          'RSI 30–40，低檔區間',
                      rsi_overbought:   'RSI > 70，超買動能延續',
                      macd_golden_cross:'MACD 柱狀圖翻正',
                      bb_lower:         '股價觸及布林下軌',
                      vol_surge:        '成交量暴增 1.5x 以上',
                      pullback:         '近 20 日回調 0–10%',
                      roe_high:         'ROE ≥ 20%，優質公司',
                      roe_ok:           'ROE 12–20%，中等品質',
                      revenue_yoy:      '營收年增 > 5%',
                      ni_yoy:           '獲利年增 > 10%',
                      debt_low:         '負債比 < 40%，財務穩健',
                      rev_yoy_6m:       '月營收連續 6 個月年增',
                      rev_yoy_3m:       '月營收連續 3 個月年增',
                      rev_mom_3m:       '月營收連續 3 個月月增',
                      rev_accel:        '月營收成長加速',
                    }
                    return (
                      <tr key={rule} className={`border-t border-slate-700/50 whitespace-nowrap ${suppressed ? 'opacity-50' : ''}`}>
                        <td className="px-3 py-2 text-slate-300 font-mono">{rule}</td>
                        <td className="px-3 py-2 text-slate-400">{RULE_LABELS[rule] ?? '—'}</td>
                        <td className="px-3 py-2 text-right text-slate-400">{s.sample_count.toLocaleString()}</td>
                        <td className={`px-3 py-2 text-right font-medium ${s.win_rate === null ? 'text-slate-500' : isMuchAbove ? 'text-green-400' : isAboveBaseline ? 'text-emerald-500' : isBelow ? 'text-red-400' : 'text-slate-300'}`}>
                          {s.win_rate !== null ? `${(s.win_rate * 100).toFixed(1)}%` : 'N/A'}
                        </td>
                        <td className={`px-3 py-2 text-right ${s.avg_excess_return_pct === null ? 'text-slate-500' : s.avg_excess_return_pct > 0 ? 'text-emerald-500' : 'text-red-400'}`}>
                          {s.avg_excess_return_pct !== null ? `${s.avg_excess_return_pct > 0 ? '+' : ''}${s.avg_excess_return_pct.toFixed(2)}%` : 'N/A'}
                        </td>
                        <td className="px-3 py-2 text-right text-slate-300">{s.score.toFixed(3)}</td>
                        <td className="px-3 py-2 text-right">
                          {s.status === 'insufficient_data'
                            ? <span className="text-yellow-500">樣本不足</span>
                            : suppressed
                              ? <span className="text-slate-500">已抑制</span>
                              : <span className="text-emerald-500">有效</span>
                          }
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}
        <button
          onClick={handleBacktest}
          disabled={backtesting}
          className="px-5 py-2 bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white text-sm rounded-lg transition-colors"
        >
          {backtesting ? `回測中… ${backtestElapsed} 秒` : '執行規則回測'}
        </button>
        {backtestDone && (
          <span className={`ml-3 text-sm ${backtestDone.includes('完成') ? 'text-green-400' : 'text-red-400'}`}>
            {backtestDone}
          </span>
        )}
        {backtestLog && (
          <pre className="mt-3 p-3 bg-slate-900 text-slate-300 text-xs rounded-lg overflow-auto max-h-48 whitespace-pre-wrap">
            {backtestLog}
          </pre>
        )}
      </section>

      {/* ── 篩選條件 ── */}
      <h2 className="text-slate-200 font-medium mb-4">推薦篩選條件</h2>
      <div className="space-y-5">
        <Field label="最低 AI 評分（0～1）">
          <input
            type="number" min="0" max="1" step="0.05"
            value={filters.scoreMin ?? ''}
            onChange={e => update('scoreMin', e.target.value ? parseFloat(e.target.value) : undefined)}
            className="input" placeholder="例如 0.6"
          />
        </Field>

        <Field label="最大本益比 (P/E)">
          <input
            type="number" min="0"
            value={filters.peRatioMax ?? ''}
            onChange={e => update('peRatioMax', e.target.value ? parseFloat(e.target.value) : undefined)}
            className="input" placeholder="例如 30"
          />
        </Field>

        <Field label="最大股價淨值比 (P/B)">
          <input
            type="number" min="0" step="0.1"
            value={filters.pbRatioMax ?? ''}
            onChange={e => update('pbRatioMax', e.target.value ? parseFloat(e.target.value) : undefined)}
            className="input" placeholder="例如 3"
          />
        </Field>

        <Field label="最低 ROE (%)">
          <input
            type="number"
            value={filters.roeMin ?? ''}
            onChange={e => update('roeMin', e.target.value ? parseFloat(e.target.value) : undefined)}
            className="input" placeholder="例如 10"
          />
        </Field>

        <Field label="最低成交量（張）">
          <input
            type="number" min="0"
            value={filters.volumeMin ?? ''}
            onChange={e => update('volumeMin', e.target.value ? parseInt(e.target.value) : undefined)}
            className="input" placeholder="例如 1000"
          />
        </Field>

        <Field label="市場">
          <div className="flex gap-3">
            {(['TSE', 'OTC'] as const).map(m => (
              <label key={m} className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={!filters.includeMarkets || filters.includeMarkets.includes(m)}
                  onChange={e => {
                    const current = filters.includeMarkets || ['TSE', 'OTC']
                    const next = e.target.checked ? [...current, m] : current.filter(x => x !== m)
                    update('includeMarkets', next.length === 2 ? undefined : next)
                  }}
                  className="accent-blue-500"
                />
                <span className="text-slate-300 text-sm">{m === 'TSE' ? '上市' : '上櫃'}</span>
              </label>
            ))}
          </div>
        </Field>

        <Field label="排除股票（以逗號分隔，含 .TW 後綴）">
          <input
            type="text"
            value={filters.excludeSymbols?.join(',') ?? ''}
            onChange={e => update('excludeSymbols', e.target.value ? e.target.value.split(',').map(s => s.trim()) : undefined)}
            className="input" placeholder="例如 2330.TW,2317.TW"
          />
        </Field>
      </div>

      <button
        onClick={handleSave}
        className="mt-6 px-6 py-2 bg-slate-700 hover:bg-slate-600 text-white rounded-lg transition-colors"
      >
        {saved ? '已儲存' : '儲存篩選條件'}
      </button>

      <style jsx>{`
        .input {
          width: 100%;
          background: rgb(30 41 59);
          border: 1px solid rgb(71 85 105);
          color: #e2e8f0;
          border-radius: 0.5rem;
          padding: 0.5rem 0.75rem;
          font-size: 0.875rem;
          outline: none;
        }
        .input:focus { border-color: rgb(59 130 246); }
        .input::placeholder { color: rgb(100 116 139); }
      `}</style>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-slate-400 text-sm mb-1.5">{label}</label>
      {children}
    </div>
  )
}
