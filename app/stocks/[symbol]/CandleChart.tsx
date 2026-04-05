'use client'
import { useEffect, useRef, useState } from 'react'
import {
  createChart, CandlestickSeries, LineSeries, HistogramSeries,
  ColorType, CrosshairMode, ISeriesApi, IChartApi,
} from 'lightweight-charts'

interface PriceRow { date: string; open: number; high: number; low: number; close: number; volume: number }
interface InstRow { date: string; foreign_net: number; trust_net: number; dealer_net: number; total_net: number }

interface Props {
  prices: PriceRow[]
  institutional: InstRow[]
  visibleMA: Record<number, boolean>
  onMaSeriesReady: (refs: Record<number, ISeriesApi<'Line'>>) => void
}

const MA_COLORS: Record<number, string> = { 5: '#facc15', 10: '#f97316', 20: '#22d3ee', 60: '#a78bfa' }

function calcMA(prices: PriceRow[], period: number) {
  const result: { time: string; value: number }[] = []
  for (let i = period - 1; i < prices.length; i++) {
    let sum = 0
    for (let j = i - period + 1; j <= i; j++) sum += prices[j].close
    result.push({ time: prices[i].date, value: sum / period })
  }
  return result
}

function fmt(v: number) {
  if (Math.abs(v) >= 10000) return (v / 10000).toFixed(1) + '萬'
  return v.toLocaleString()
}

export default function CandleChart({ prices, institutional, visibleMA, onMaSeriesReady }: Props) {
  const priceRef = useRef<HTMLDivElement>(null)
  const volRef = useRef<HTMLDivElement>(null)
  const foreignRef = useRef<HTMLDivElement>(null)
  const trustRef = useRef<HTMLDivElement>(null)
  const chartsRef = useRef<IChartApi[]>([])
  const maSeriesRef = useRef<Record<number, ISeriesApi<'Line'>>>({})

  // Crosshair tooltip state (K 棒 OHLC)
  const [priceTooltip, setPriceTooltip] = useState<{
    x: number; date: string; open: number; high: number; low: number; close: number; volume: number; up: boolean; changePct: number | null
  } | null>(null)

  // Tooltip state
  const [instTooltip, setInstTooltip] = useState<{
    x: number; y: number; date: string; foreign: number; trust: number
  } | null>(null)

  // Build a date → price map for crosshair tooltip
  const priceMapRef = useRef<Map<string, PriceRow>>(new Map())
  const priceIdxMapRef = useRef<Map<string, number>>(new Map())

  // Build a date → inst map for quick lookup
  const instMapRef = useRef<Map<string, InstRow>>(new Map())

  useEffect(() => {
    priceMapRef.current = new Map(prices.map(p => [p.date, p]))
    priceIdxMapRef.current = new Map(prices.map((p, i) => [p.date, i]))
  }, [prices])

  useEffect(() => {
    instMapRef.current = new Map(institutional.map(d => [d.date, d]))
  }, [institutional])

  useEffect(() => {
    if (!priceRef.current || !volRef.current) return

    const common = {
      layout: { background: { type: ColorType.Solid, color: '#1e293b' }, textColor: '#94a3b8' },
      grid: { vertLines: { color: '#334155' }, horzLines: { color: '#334155' } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#475569' },
      timeScale: { borderColor: '#475569', timeVisible: false },
    }

    // ── K 線圖 ──
    const priceChart = createChart(priceRef.current, { ...common, height: 340 })
    const candleSeries = priceChart.addSeries(CandlestickSeries, {
      upColor: '#ef4444', downColor: '#22c55e',
      borderUpColor: '#ef4444', borderDownColor: '#22c55e',
      wickUpColor: '#ef4444', wickDownColor: '#22c55e',
    })
    candleSeries.setData(prices.map(p => ({ time: p.date, open: p.open, high: p.high, low: p.low, close: p.close })))

    // Crosshair → OHLC tooltip
    priceChart.subscribeCrosshairMove(param => {
      if (!param.time || !param.sourceEvent) { setPriceTooltip(null); return }
      const date = param.time as string
      const row = priceMapRef.current.get(date)
      if (!row) { setPriceTooltip(null); return }
      const rect = priceRef.current!.getBoundingClientRect()
      const x = (param.sourceEvent as unknown as MouseEvent).clientX - rect.left
      const idx = priceIdxMapRef.current.get(date) ?? -1
      const prevClose = idx > 0 ? prices[idx - 1].close : null
      const changePct = prevClose ? (row.close - prevClose) / prevClose * 100 : null
      setPriceTooltip({ x, date, open: row.open, high: row.high, low: row.low, close: row.close, volume: row.volume, up: row.close >= row.open, changePct })
    })

    const maRefs: Record<number, ISeriesApi<'Line'>> = {}
    for (const period of [5, 10, 20, 60]) {
      const s = priceChart.addSeries(LineSeries, {
        color: MA_COLORS[period], lineWidth: 1,
        priceLineVisible: false, lastValueVisible: false,
      })
      s.setData(calcMA(prices, period))
      s.applyOptions({ visible: visibleMA[period] ?? true })
      maRefs[period] = s
    }
    maSeriesRef.current = maRefs
    onMaSeriesReady(maRefs)

    // ── 成交量 ──
    const volChart = createChart(volRef.current!, { ...common, height: 80 })
    const volSeries = volChart.addSeries(HistogramSeries, {
      priceFormat: { type: 'custom', formatter: (v: number) => Math.round(v).toLocaleString() },
    })
    volSeries.setData(prices.map((p, i) => ({
      time: p.date, value: p.volume,
      color: i > 0 && p.close >= prices[i - 1].close ? '#ef444466' : '#22c55e66',
    })))

    const allCharts: IChartApi[] = [priceChart, volChart]

    // ── 外資 ──
    let foreignChart: IChartApi | null = null
    if (institutional.length > 0 && foreignRef.current) {
      foreignChart = createChart(foreignRef.current, { ...common, height: 90 })
      const foreignSeries = foreignChart.addSeries(HistogramSeries, { base: 0 })
      foreignSeries.setData(institutional.map(d => ({
        time: d.date, value: Math.round(d.foreign_net / 1000),
        color: d.foreign_net >= 0 ? '#60a5fa' : '#f472b6',
      })))
      allCharts.push(foreignChart)

      // Tooltip on crosshair move
      foreignChart.subscribeCrosshairMove(param => {
        if (!param.time || !param.sourceEvent) {
          setInstTooltip(null)
          return
        }
        const date = param.time as string
        const row = instMapRef.current.get(date)
        if (!row) { setInstTooltip(null); return }
        const rect = foreignRef.current!.getBoundingClientRect()
        setInstTooltip({
          x: (param.sourceEvent as unknown as MouseEvent).clientX - rect.left,
          y: 0,
          date,
          foreign: Math.round(row.foreign_net / 1000),
          trust: Math.round(row.trust_net / 1000),
        })
      })
    }

    // ── 投信 ──
    let trustChart: IChartApi | null = null
    if (institutional.length > 0 && trustRef.current) {
      trustChart = createChart(trustRef.current, { ...common, height: 90 })
      const trustSeries = trustChart.addSeries(HistogramSeries, { base: 0 })
      trustSeries.setData(institutional.map(d => ({
        time: d.date, value: Math.round(d.trust_net / 1000),
        color: d.trust_net >= 0 ? '#34d399' : '#fb923c',
      })))
      allCharts.push(trustChart)

      trustChart.subscribeCrosshairMove(param => {
        if (!param.time || !param.sourceEvent) {
          setInstTooltip(null)
          return
        }
        const date = param.time as string
        const row = instMapRef.current.get(date)
        if (!row) { setInstTooltip(null); return }
        const rect = trustRef.current!.getBoundingClientRect()
        setInstTooltip({
          x: (param.sourceEvent as unknown as MouseEvent).clientX - rect.left,
          y: 0,
          date,
          foreign: Math.round(row.foreign_net / 1000),
          trust: Math.round(row.trust_net / 1000),
        })
      })
    }

    // 雙向同步時間軸：任一圖拖動，其他全部跟上
    let syncing = false
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    function syncFrom(source: IChartApi, targets: (IChartApi | null)[]) {
      source.timeScale().subscribeVisibleLogicalRangeChange((range: any) => {
        if (syncing || !range) return
        syncing = true
        targets.forEach(t => t?.timeScale().setVisibleLogicalRange(range))
        syncing = false
      })
    }
    syncFrom(priceChart, [volChart, foreignChart, trustChart])
    syncFrom(volChart, [priceChart, foreignChart, trustChart])
    if (foreignChart) syncFrom(foreignChart, [priceChart, volChart, trustChart])
    if (trustChart) syncFrom(trustChart, [priceChart, volChart, foreignChart])

    priceChart.timeScale().fitContent()
    allCharts.forEach(c => { if (c !== priceChart) c.timeScale().fitContent() })

    chartsRef.current = allCharts

    return () => {
      chartsRef.current.forEach(c => c.remove())
      chartsRef.current = []
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prices, institutional])

  // MA 切換
  useEffect(() => {
    for (const [p, s] of Object.entries(maSeriesRef.current)) {
      s.applyOptions({ visible: visibleMA[Number(p)] ?? true })
    }
  }, [visibleMA])

  return (
    <div>
      <div className="relative">
        <div ref={priceRef} className="w-full" />
        {priceTooltip && (
          <div
            className="absolute top-1 left-1 pointer-events-none px-2 py-1 rounded text-xs z-10"
            style={{ background: 'rgba(15,23,42,0.90)', border: '1px solid #334155', maxWidth: 'calc(100% - 8px)' }}
          >
            <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
              <span className="text-slate-500">{priceTooltip.date}</span>
              {priceTooltip.changePct != null && (
                <span style={{ color: priceTooltip.changePct >= 0 ? '#ef4444' : '#22c55e' }}>
                  {priceTooltip.changePct >= 0 ? '+' : ''}{priceTooltip.changePct.toFixed(2)}%
                </span>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 mt-0.5">
              <span className="text-slate-500">開</span><span style={{ color: priceTooltip.up ? '#ef4444' : '#22c55e' }}>{priceTooltip.open.toFixed(2)}</span>
              <span className="text-slate-500">高</span><span style={{ color: priceTooltip.up ? '#ef4444' : '#22c55e' }}>{priceTooltip.high.toFixed(2)}</span>
              <span className="text-slate-500">低</span><span style={{ color: priceTooltip.up ? '#ef4444' : '#22c55e' }}>{priceTooltip.low.toFixed(2)}</span>
              <span className="text-slate-500">收</span><span style={{ color: priceTooltip.up ? '#ef4444' : '#22c55e' }}>{priceTooltip.close.toFixed(2)}</span>
              <span className="text-slate-500">量</span><span className="text-slate-400">{priceTooltip.volume.toLocaleString()}</span>
            </div>
          </div>
        )}
      </div>
      <div className="mt-1 px-1 text-xs text-slate-500">成交量（張）</div>
      <div ref={volRef} className="w-full" />

      {institutional.length > 0 && (
        <>
          {/* 外資 */}
          <div className="mt-2 px-1 flex items-center gap-2 text-xs">
            <span className="w-2 h-2 rounded-sm inline-block" style={{ background: '#60a5fa' }} />
            <span className="text-slate-300">外資買賣超（張）</span>
          </div>
          <div ref={foreignRef} className="w-full relative">
            {instTooltip && (
              <div
                className="absolute z-10 pointer-events-none px-2 py-1.5 rounded text-xs whitespace-nowrap"
                style={{
                  left: Math.min(instTooltip.x + 12, (foreignRef.current?.clientWidth ?? 400) - 160),
                  top: 8,
                  background: '#0f172a',
                  border: '1px solid #475569',
                }}
              >
                <div className="text-slate-400 mb-1">{instTooltip.date}</div>
                <div style={{ color: instTooltip.foreign >= 0 ? '#60a5fa' : '#f472b6' }}>
                  外資：{instTooltip.foreign >= 0 ? '+' : ''}{fmt(instTooltip.foreign)} 張
                </div>
                <div style={{ color: instTooltip.trust >= 0 ? '#34d399' : '#fb923c' }}>
                  投信：{instTooltip.trust >= 0 ? '+' : ''}{fmt(instTooltip.trust)} 張
                </div>
              </div>
            )}
          </div>

          {/* 投信 */}
          <div className="mt-1 px-1 flex items-center gap-2 text-xs">
            <span className="w-2 h-2 rounded-sm inline-block" style={{ background: '#34d399' }} />
            <span className="text-slate-300">投信買賣超（張）</span>
          </div>
          <div ref={trustRef} className="w-full relative" />
        </>
      )}
    </div>
  )
}
