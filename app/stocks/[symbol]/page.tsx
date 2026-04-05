'use client'
import { useState, useEffect, useRef } from 'react'
import { use } from 'react'
import dynamic from 'next/dynamic'
import { ISeriesApi } from 'lightweight-charts'

const CandleChart = dynamic(() => import('./CandleChart'), { ssr: false })

interface PriceRow { date: string; open: number; high: number; low: number; close: number; volume: number }
interface InstRow { date: string; foreign_net: number; trust_net: number; dealer_net: number; total_net: number }
interface ScoreRow { date: string; score: number; signal: string }
interface StockDetail {
  symbol: string; name: string; market: string; industry?: string
  prices: PriceRow[]
  financials: { year: number; quarter: number; revenue?: number; net_income?: number; eps?: number }[]
  institutional: InstRow[]
  scoreHistory: ScoreRow[]
}

const MA_COLORS: Record<number, string> = { 5: '#facc15', 10: '#f97316', 20: '#22d3ee', 60: '#a78bfa' }

export default function StockPage({ params }: { params: Promise<{ symbol: string }> }) {
  const { symbol } = use(params)
  const [data, setData] = useState<StockDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [visibleMA, setVisibleMA] = useState<Record<number, boolean>>({ 5: true, 10: true, 20: true, 60: true })
  const maSeriesRef = useRef<Record<number, ISeriesApi<'Line'>>>({})

  useEffect(() => {
    const encoded = encodeURIComponent(symbol)
    fetch(`/api/stocks/${encoded}`)
      .then(r => r.json())
      .then(setData)
      .finally(() => setLoading(false))
  }, [symbol])

  function toggleMA(period: number) {
    const next = !visibleMA[period]
    setVisibleMA(v => ({ ...v, [period]: next }))
    maSeriesRef.current[period]?.applyOptions({ visible: next })
  }

  if (loading) return <div className="text-slate-400 py-20 text-center">載入中...</div>
  if (!data || 'error' in data) return <div className="text-slate-400 py-20 text-center">找不到股票</div>

  const displaySymbol = symbol.replace('.TW', '').replace('.TWO', '')
  const latest = data.prices[data.prices.length - 1]
  const prev = data.prices[data.prices.length - 2]
  const changePct = prev ? ((latest.close - prev.close) / prev.close) * 100 : 0

  return (
    <div className="max-w-5xl">
      <div className="mb-4">
        <a href="/" className="text-slate-400 hover:text-slate-200 text-sm">← 返回推薦清單</a>
      </div>

      {/* 標題 */}
      <div className="flex items-start gap-4 mb-6">
        <div>
          <h1 className="text-2xl font-bold text-white">{displaySymbol} {data.name}</h1>
          <div className="flex gap-3 mt-1">
            <span className={`text-xs px-2 py-0.5 rounded ${data.market === 'TSE' ? 'bg-blue-900/50 text-blue-300' : 'bg-purple-900/50 text-purple-300'}`}>
              {data.market === 'TSE' ? '上市' : '上櫃'}
            </span>
            {data.industry && <span className="text-slate-400 text-sm">{data.industry}</span>}
          </div>
        </div>
        {latest && (
          <div className="ml-auto text-right">
            <div className="text-3xl font-mono font-bold text-white">{latest.close.toFixed(2)}</div>
            <div className={`text-sm font-mono ${changePct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
              {changePct >= 0 ? '+' : ''}{changePct.toFixed(2)}%
            </div>
          </div>
        )}
      </div>

      {/* 圖表 */}
      {data.prices.length > 0 && (
        <div className="bg-slate-800 rounded-xl p-4 mb-6">
          {/* MA 按鈕 */}
          <div className="flex items-center gap-3 mb-3">
            <span className="text-slate-400 text-xs">均線</span>
            {([5, 10, 20, 60] as const).map(p => (
              <button
                key={p}
                onClick={() => toggleMA(p)}
                className="text-xs px-2 py-0.5 rounded border transition-opacity"
                style={{
                  borderColor: MA_COLORS[p],
                  color: MA_COLORS[p],
                  opacity: visibleMA[p] ? 1 : 0.3,
                }}
              >
                MA{p}
              </button>
            ))}
          </div>

          <CandleChart
            prices={data.prices}
            institutional={data.institutional}
            visibleMA={visibleMA}
            onMaSeriesReady={refs => { maSeriesRef.current = refs }}
          />
        </div>
      )}

      {/* 分數走勢 */}
      {data.scoreHistory && data.scoreHistory.length > 0 && (
        <div className="bg-slate-800 rounded-xl p-4 mb-6">
          <h2 className="text-slate-300 text-sm font-medium mb-3">評分走勢</h2>
          <ScoreHistoryChart history={data.scoreHistory} />
        </div>
      )}

      {/* 財務資料 */}
      {data.financials.length > 0 && (
        <div className="bg-slate-800 rounded-xl p-4">
          <h2 className="text-slate-300 text-sm font-medium mb-3">季度財務資料</h2>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-slate-400 border-b border-slate-700">
                <th className="text-left py-2 px-3">季度</th>
                <th className="text-right py-2 px-3">營收</th>
                <th className="text-right py-2 px-3">淨利</th>
                <th className="text-right py-2 px-3">EPS</th>
              </tr>
            </thead>
            <tbody>
              {data.financials.map((f, i) => (
                <tr key={i} className="border-b border-slate-700/50">
                  <td className="py-2 px-3 text-slate-300">{f.year}Q{f.quarter}</td>
                  <td className="py-2 px-3 text-right font-mono text-slate-300">
                    {f.revenue ? (f.revenue / 1e8).toFixed(2) + '億' : '-'}
                  </td>
                  <td className="py-2 px-3 text-right font-mono text-slate-300">
                    {f.net_income ? (f.net_income / 1e8).toFixed(2) + '億' : '-'}
                  </td>
                  <td className={`py-2 px-3 text-right font-mono ${(f.eps ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {f.eps?.toFixed(2) ?? '-'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

const SIGNAL_COLOR: Record<string, string> = {
  buy: '#22c55e',
  watch: '#eab308',
  neutral: '#64748b',
}

function ScoreHistoryChart({ history }: { history: ScoreRow[] }) {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!containerRef.current || history.length === 0) return
    // dynamic import to avoid SSR
    let chart: import('lightweight-charts').IChartApi | null = null
    import('lightweight-charts').then(({ createChart, ColorType, LineSeries }) => {
      if (!containerRef.current) return
      chart = createChart(containerRef.current, {
        layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: '#94a3b8' },
        grid: { vertLines: { color: '#1e293b' }, horzLines: { color: '#1e293b' } },
        rightPriceScale: { borderColor: '#334155' },
        timeScale: { borderColor: '#334155', timeVisible: true },
        height: 160,
      })

      const series = chart.addSeries(LineSeries, {
        color: '#818cf8',
        lineWidth: 2,
        priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
        crosshairMarkerVisible: true,
      })

      series.setData(
        history.map(h => ({ time: h.date as import('lightweight-charts').Time, value: Math.round(h.score * 100) / 100 }))
      )

      chart.timeScale().fitContent()
    })
    return () => { chart?.remove() }
  }, [history])

  return (
    <div>
      <div ref={containerRef} />
      <div className="flex gap-3 mt-2">
        {Object.entries({ buy: '買入', watch: '觀察', neutral: '中立' }).map(([sig, label]) => (
          <span key={sig} className="flex items-center gap-1 text-[11px] text-slate-400">
            <span className="inline-block w-2 h-2 rounded-full" style={{ background: SIGNAL_COLOR[sig] }} />
            {label}
          </span>
        ))}
      </div>
    </div>
  )
}
