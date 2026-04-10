'use client'
import { useState, useEffect, useRef } from 'react'
import { use } from 'react'
import dynamic from 'next/dynamic'
import { ISeriesApi } from 'lightweight-charts'

const CandleChart = dynamic(() => import('./CandleChart'), { ssr: false })

interface PriceRow { date: string; open: number; high: number; low: number; close: number; volume: number }
interface InstRow { date: string; foreign_net: number; trust_net: number; dealer_net: number; total_net: number }
interface StockDetail {
  symbol: string; name: string; market: string; industry?: string
  prices: PriceRow[]
  financials: { year: number; quarter: number; revenue?: number; net_income?: number; eps?: number }[]
  institutional: InstRow[]
}

const MA_COLORS: Record<number, string> = { 5: '#facc15', 10: '#f97316', 20: '#22d3ee', 60: '#a78bfa' }

interface NewsItem { title: string; link: string; pubDate: string; source: string }

export default function StockPage({ params }: { params: Promise<{ symbol: string }> }) {
  const { symbol } = use(params)
  const [data, setData] = useState<StockDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [visibleMA, setVisibleMA] = useState<Record<number, boolean>>({ 5: true, 10: true, 20: true, 60: true })
  const maSeriesRef = useRef<Record<number, ISeriesApi<'Line'>>>({})
  const [news, setNews] = useState<NewsItem[]>([])

  useEffect(() => {
    if (!symbol) return
    fetch(`/api/stocks/${encodeURIComponent(symbol)}/news`)
      .then(r => r.json())
      .then(d => setNews(d.items || []))
      .catch(() => {})
  }, [symbol])

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

  if (loading) return (
    <div className="w-full">
      <div className="mb-1">
        <a href="/" className="text-slate-500 hover:text-slate-300 text-xs">← 返回</a>
      </div>
      <div className="animate-pulse">
        <div className="h-6 w-40 bg-slate-800 rounded mb-2" />
        <div className="flex gap-2">
          <div className="flex-1 bg-slate-800 rounded-lg h-[500px]" />
          <div className="w-64 bg-slate-800 rounded-lg h-[500px] hidden lg:block" />
        </div>
      </div>
    </div>
  )
  if (!data || 'error' in data) return <div className="text-slate-500 py-10 text-center text-sm">找不到股票</div>

  const displaySymbol = symbol.replace('.TW', '').replace('.TWO', '')
  const latest = data.prices[data.prices.length - 1]
  const prev = data.prices[data.prices.length - 2]
  const changePct = prev ? ((latest.close - prev.close) / prev.close) * 100 : 0

  return (
    <div className="w-full">
      {/* 頂部：返回 + 股名 + 價格，一行搞定 */}
      <div className="flex items-baseline gap-2 mb-2">
        <a href="/" className="text-slate-500 hover:text-slate-300 text-xs shrink-0">←</a>
        <h1 className="text-base font-bold text-white">{displaySymbol} {data.name}</h1>
        <span className={`text-[10px] px-1 py-0.5 rounded ${data.market === 'TSE' ? 'bg-blue-900/50 text-blue-300' : 'bg-purple-900/50 text-purple-300'}`}>
          {data.market === 'TSE' ? '上市' : '上櫃'}
        </span>
        {latest && (
          <>
            <span className="text-slate-600 mx-1">|</span>
            <span className="text-base font-mono font-bold text-white">{latest.close.toFixed(2)}</span>
            <span className={`text-sm font-mono ${changePct >= 0 ? 'text-red-400' : 'text-green-400'}`}>
              {changePct >= 0 ? '+' : ''}{changePct.toFixed(2)}%
            </span>
            <span className="text-slate-600 mx-1">|</span>
            <span className="text-xs text-slate-400">量 <span className="font-mono">{latest.volume.toLocaleString()}</span></span>
          </>
        )}
      </div>

      {/* 主體：圖表 + 右側面板 */}
      <div className="flex flex-col lg:flex-row gap-3">
        {/* 圖表 */}
        {data.prices.length > 0 && (
          <div className="lg:w-[60%] xl:w-[65%] min-w-0 bg-slate-800 rounded-lg p-2 overflow-hidden">
            <div className="flex items-center gap-1.5 mb-1">
              {([5, 10, 20, 60] as const).map(p => (
                <button
                  key={p}
                  onClick={() => toggleMA(p)}
                  className="text-[10px] px-1.5 py-0.5 rounded border transition-opacity"
                  style={{ borderColor: MA_COLORS[p], color: MA_COLORS[p], opacity: visibleMA[p] ? 1 : 0.3 }}
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

        {/* 右側：財報 + 新聞 */}
        <div className="flex-1 min-w-0 flex flex-col gap-2">
          {data.financials.length > 0 && (
            <div className="bg-slate-800 rounded-lg p-2">
              <h2 className="text-slate-500 text-[11px] font-medium mb-1">季度財務</h2>
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-slate-500 border-b border-slate-700/60">
                    <th className="text-left py-1 px-1.5 font-medium">季度</th>
                    <th className="text-right py-1 px-1.5 font-medium">營收</th>
                    <th className="text-right py-1 px-1.5 font-medium">淨利</th>
                    <th className="text-right py-1 px-1.5 font-medium">EPS</th>
                  </tr>
                </thead>
                <tbody>
                  {data.financials.map((f, i) => (
                    <tr key={i} className="border-b border-slate-700/20">
                      <td className="py-1 px-1.5 text-slate-400">{f.year}Q{f.quarter}</td>
                      <td className="py-1 px-1.5 text-right font-mono text-slate-300">
                        {f.revenue ? (f.revenue / 1e8).toFixed(1) + '億' : '-'}
                      </td>
                      <td className="py-1 px-1.5 text-right font-mono text-slate-300">
                        {f.net_income ? (f.net_income / 1e8).toFixed(1) + '億' : '-'}
                      </td>
                      <td className={`py-1 px-1.5 text-right font-mono ${(f.eps ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                        {f.eps?.toFixed(2) ?? '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {news.length > 0 && (
            <div className="bg-slate-800 rounded-lg p-2 flex flex-col flex-1 min-h-0">
              <h2 className="text-slate-500 text-[11px] font-medium mb-1 shrink-0">新聞</h2>
              <ul className="space-y-1.5 overflow-y-auto flex-1">
                {news.map((n, i) => (
                  <li key={i} className="border-b border-slate-700/20 pb-1.5 last:border-0 last:pb-0">
                    <a href={n.link} target="_blank" rel="noopener noreferrer"
                      className="text-slate-200 hover:text-white text-[11px] leading-tight block">
                      {n.title}
                    </a>
                    <span className="text-slate-600 text-[10px]">
                      {n.source} · {n.pubDate ? new Date(n.pubDate).toLocaleDateString('zh-TW') : ''}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
