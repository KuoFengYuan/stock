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

  if (loading) return <div className="text-slate-400 py-20 text-center">載入中...</div>
  if (!data || 'error' in data) return <div className="text-slate-400 py-20 text-center">找不到股票</div>

  const displaySymbol = symbol.replace('.TW', '').replace('.TWO', '')
  const latest = data.prices[data.prices.length - 1]
  const prev = data.prices[data.prices.length - 2]
  const changePct = prev ? ((latest.close - prev.close) / prev.close) * 100 : 0

  return (
    <div className="w-full">
      <div className="mb-4">
        <a href="/" className="text-slate-400 hover:text-slate-200 text-sm">← 返回推薦清單</a>
      </div>

      {/* 標題 */}
      <div className="flex items-start gap-4 mb-4">
        <div className="flex-1 min-w-0">
          <h1 className="text-xl sm:text-2xl font-bold text-white">{displaySymbol} {data.name}</h1>
          <div className="flex gap-3 mt-1 flex-wrap">
            <span className={`text-xs px-2 py-0.5 rounded ${data.market === 'TSE' ? 'bg-blue-900/50 text-blue-300' : 'bg-purple-900/50 text-purple-300'}`}>
              {data.market === 'TSE' ? '上市' : '上櫃'}
            </span>
            {data.industry && <span className="text-slate-400 text-sm">{data.industry}</span>}
          </div>
        </div>
        {latest && (
          <div className="text-right shrink-0">
            <div className="text-2xl sm:text-3xl font-mono font-bold text-white">{latest.close.toFixed(2)}</div>
            <div className={`text-sm font-mono ${changePct >= 0 ? 'text-red-400' : 'text-green-400'}`}>
              {changePct >= 0 ? '+' : ''}{changePct.toFixed(2)}%
            </div>
          </div>
        )}
      </div>

      {/* 主體：桌面左右分欄，手機上下；財報在手機排最後 */}
      <div className="flex flex-wrap lg:flex-nowrap gap-4">

        {/* 左欄：圖表（手機 order-1，桌面正常） */}
        <div className="w-full lg:flex-1 lg:min-w-0 order-1">
          {data.prices.length > 0 && (
            <div className="bg-slate-800 rounded-xl p-4">
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
        </div>

        {/* 右欄：新聞（手機 order-2，桌面正常） */}
        {news.length > 0 && (
          <div className="w-full lg:w-72 xl:w-80 shrink-0 order-2 lg:order-none">
            <div className="bg-slate-800 rounded-xl p-4 lg:sticky lg:top-4 flex flex-col" style={{ maxHeight: '80vh' }}>
              <h2 className="text-slate-300 text-sm font-medium mb-3 shrink-0">最新新聞</h2>
              <ul className="space-y-3 overflow-y-auto pr-1">
                {news.map((n, i) => (
                  <li key={i} className="border-b border-slate-700/50 pb-3 last:border-0 last:pb-0">
                    <a href={n.link} target="_blank" rel="noopener noreferrer"
                      className="text-slate-200 hover:text-white text-sm leading-snug block mb-1">
                      {n.title}
                    </a>
                    <span className="text-slate-500 text-xs">
                      {n.source} · {n.pubDate ? new Date(n.pubDate).toLocaleDateString('zh-TW') : ''}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}

        {/* 財務資料（手機 order-3 排最後，桌面撐滿底部） */}
        {data.financials.length > 0 && (
          <div className="w-full order-3">
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
          </div>
        )}
      </div>
    </div>
  )
}
