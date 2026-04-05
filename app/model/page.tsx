'use client'
import { useState, useEffect } from 'react'

interface FeatureItem { name: string; importance: number }
interface FeatureData { auc: number; features: FeatureItem[] }

const FEATURE_LABELS: Record<string, string> = {
  revenue_yoy: '營收年增率',
  roe: 'ROE',
  ni_yoy: '淨利年增率',
  eps_ttm: 'EPS(TTM)',
  return60d: '60日報酬',
  pe_ratio: 'PE比',
  margin_balance_chg: '融資增減',
  debt_ratio: '負債比',
  atr_pct: '波動率(ATR)',
  sma60_bias: '60日乖離率',
  pb_ratio: 'PB比',
  return20d: '20日報酬',
  rsi14: 'RSI14',
  vol_ratio: '量比',
  sma20_bias: '20日乖離率',
  short_balance_chg: '融券增減',
  bb_pos: 'BB位置',
}

export default function ModelPage() {
  const [data, setData] = useState<FeatureData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetch('/api/feature-importance')
      .then(r => r.json())
      .then(d => {
        if (d.error) setError(d.error)
        else setData(d)
      })
      .catch(e => setError(String(e)))
      .finally(() => setLoading(false))
  }, [])

  if (loading) return <div className="text-slate-400 py-20 text-center">載入中...</div>
  if (error) return <div className="text-red-400 py-20 text-center">錯誤：{error}</div>
  if (!data) return null

  const maxImportance = data.features[0]?.importance ?? 1

  return (
    <div className="max-w-2xl">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-white">模型特徵重要性</h1>
        <p className="text-slate-400 text-sm mt-1">
          XGBoost 分類器　AUC = <span className="text-white font-mono">{data.auc.toFixed(4)}</span>
          　共 {data.features.length} 個特徵
        </p>
      </div>

      <div className="bg-slate-800 rounded-xl p-5 space-y-3">
        {data.features.map((f, i) => {
          const pct = f.importance / maxImportance
          const color = pct >= 0.8 ? '#6366f1' : pct >= 0.5 ? '#818cf8' : '#475569'
          const label = FEATURE_LABELS[f.name] ?? f.name
          return (
            <div key={f.name}>
              <div className="flex items-center justify-between mb-1">
                <div className="flex items-center gap-2">
                  <span className="text-slate-500 text-xs w-4 text-right">{i + 1}</span>
                  <span className="text-slate-200 text-sm">{label}</span>
                  <span className="text-slate-500 text-xs font-mono">{f.name}</span>
                </div>
                <span className="text-slate-300 text-xs font-mono">{(f.importance * 100).toFixed(1)}%</span>
              </div>
              <div className="h-2 bg-slate-700 rounded-full overflow-hidden">
                <div
                  className="h-full rounded-full transition-all"
                  style={{ width: `${pct * 100}%`, background: color }}
                />
              </div>
            </div>
          )
        })}
      </div>

      <div className="mt-4 p-4 bg-slate-800/50 rounded-xl text-xs text-slate-500 space-y-1">
        <p>特徵重要性基於 XGBoost gain，反映各特徵對分裂點的平均資訊增益。</p>
        <p>重要性高的特徵對模型預測影響最大，可作為資料品質監控指標。</p>
      </div>
    </div>
  )
}
