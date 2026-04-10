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

  // Crosshair tooltip state (K 棒 OHLC + 法人)
  const [priceTooltip, setPriceTooltip] = useState<{
    x: number; date: string; open: number; high: number; low: number; close: number; volume: number; up: boolean; changePct: number | null
    foreign: number | null; trust: number | null
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
      layout: { background: { type: ColorType.Solid, color: '#1e293b' }, textColor: '#94a3b8', attributionLogo: false },
      grid: { vertLines: { color: '#334155' }, horzLines: { color: '#334155' } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#475569' },
      timeScale: { borderColor: '#475569', timeVisible: false, barSpacing: 8, minBarSpacing: 8 },
      handleScroll: false,
      handleScale: false,
    }

    // ── K 線圖 ──
    const priceChart = createChart(priceRef.current, { ...common, height: 260 })
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
      const inst = instMapRef.current.get(date)
      const foreign = inst ? Math.round(inst.foreign_net / 1000) : null
      const trust = inst ? Math.round(inst.trust_net / 1000) : null
      setPriceTooltip({ x, date, open: row.open, high: row.high, low: row.low, close: row.close, volume: row.volume, up: row.close >= row.open, changePct, foreign, trust })
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
    const volChart = createChart(volRef.current!, { ...common, height: 60 })
    const volSeries = volChart.addSeries(HistogramSeries, {
      priceFormat: { type: 'custom', formatter: (v: number) => Math.round(v).toLocaleString() },
      lastValueVisible: false, priceLineVisible: false,
    })
    volSeries.setData(prices.map((p, i) => ({
      time: p.date, value: p.volume,
      color: i > 0 && p.close >= prices[i - 1].close ? '#ef444466' : '#22c55e66',
    })))

    const allCharts: IChartApi[] = [priceChart, volChart]

    // 對齊時間軸：用 prices 日期補齊 institutional 缺漏的日期（填 0）
    const instByDate = new Map(institutional.map(d => [d.date, d]))
    const alignedInst = prices.map(p => instByDate.get(p.date) ?? { date: p.date, foreign_net: 0, trust_net: 0, dealer_net: 0, total_net: 0 })

    // ── 外資 ──
    let foreignChart: IChartApi | null = null
    if (institutional.length > 0 && foreignRef.current) {
      foreignChart = createChart(foreignRef.current, { ...common, height: 70 })
      const foreignSeries = foreignChart.addSeries(HistogramSeries, { base: 0, lastValueVisible: false, priceLineVisible: false })
      foreignSeries.setData(alignedInst.map(d => ({
        time: d.date, value: Math.round(d.foreign_net / 1000),
        color: d.foreign_net >= 0 ? '#60a5fa' : '#f472b6',
      })))
      allCharts.push(foreignChart)
    }

    // ── 投信 ──
    let trustChart: IChartApi | null = null
    if (institutional.length > 0 && trustRef.current) {
      trustChart = createChart(trustRef.current, { ...common, height: 70 })
      const trustSeries = trustChart.addSeries(HistogramSeries, { base: 0, lastValueVisible: false, priceLineVisible: false })
      trustSeries.setData(alignedInst.map(d => ({
        time: d.date, value: Math.round(d.trust_net / 1000),
        color: d.trust_net >= 0 ? '#34d399' : '#fb923c',
      })))
      allCharts.push(trustChart)
    }

    // 雙向同步時間軸（用實際時間範圍，不用 logical index 避免資料量不同錯位）
    // 同步所有圖表的時間軸（用 visibleRange 而非 scrollPosition，確保日期對齊）
    let syncing = false
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    function syncFrom(source: IChartApi, targets: (IChartApi | null)[]) {
      source.timeScale().subscribeVisibleTimeRangeChange((range: any) => {
        if (syncing || !range) return
        syncing = true
        targets.forEach(t => { try { t?.timeScale().setVisibleRange(range) } catch {} })
        syncing = false
      })
    }
    syncFrom(priceChart, [volChart, foreignChart, trustChart])
    syncFrom(volChart, [priceChart, foreignChart, trustChart])
    if (foreignChart) syncFrom(foreignChart, [priceChart, volChart, trustChart])
    if (trustChart) syncFrom(trustChart, [priceChart, volChart, foreignChart])

    if (prices.length > 0) {
      const last = prices[prices.length - 1].date
      const from = prices[Math.max(0, prices.length - 250)].date
      allCharts.forEach(c => c.timeScale().setVisibleRange({ from, to: last }))
    } else {
      priceChart.timeScale().fitContent()
      allCharts.forEach(c => { if (c !== priceChart) c.timeScale().fitContent() })
    }

    chartsRef.current = allCharts

    // 自訂滾動：滾輪 + 滑鼠拖曳 + 觸控拖曳
    const containers = [priceRef.current, volRef.current, foreignRef.current, trustRef.current].filter(Boolean) as HTMLDivElement[]
    const visibleBars = 250
    const maxLeftScroll = -(prices.length - visibleBars)

    // 記錄初始 scrollPosition（setVisibleRange 後的值），作為右邊界
    const initialPos = priceChart.timeScale().scrollPosition()

    function getScrollBounds() {
      // scrollPosition: 正數=最新K棒右邊有空白，負數=往左捲了
      // 右邊界：不超過初始位置（最新K棒在最右邊），不允許出現空白
      return { left: maxLeftScroll, right: initialPos }
    }

    let rafId = 0
    let pendingPos: number | null = null
    let lastAppliedPos: number | null = null

    const BAR_SPACING = 8

    function applyScroll(rawPos: number) {
      const bounds = getScrollBounds()
      const newPos = Math.max(bounds.left, Math.min(bounds.right, rawPos))
      if (lastAppliedPos !== null && Math.abs(newPos - lastAppliedPos) < 0.01) return
      pendingPos = newPos
      if (!rafId) {
        rafId = requestAnimationFrame(() => {
          if (pendingPos !== null) {
            allCharts.forEach(c => {
              c.timeScale().scrollToPosition(pendingPos!, false)
              c.timeScale().applyOptions({ barSpacing: BAR_SPACING })
            })
            lastAppliedPos = pendingPos
          }
          rafId = 0
          pendingPos = null
        })
      }
    }

    // 滾輪
    const wheelHandler = (e: WheelEvent) => {
      e.preventDefault()
      const pos = priceChart.timeScale().scrollPosition()
      applyScroll(pos - Math.sign(e.deltaY) * 3)
    }

    // 滑鼠拖曳
    let dragging = false
    let dragStartX = 0
    let dragStartPos = 0
    const mouseDown = (e: MouseEvent) => { dragging = true; dragStartX = e.clientX; dragStartPos = priceChart.timeScale().scrollPosition() }
    const mouseMove = (e: MouseEvent) => {
      if (!dragging) return
      applyScroll(dragStartPos + (e.clientX - dragStartX) / 8)
    }
    const mouseUp = () => { dragging = false }

    // 觸控拖曳
    let touchStartX = 0
    let touchStartPos = 0
    const touchStart = (e: TouchEvent) => { touchStartX = e.touches[0].clientX; touchStartPos = priceChart.timeScale().scrollPosition() }
    const touchMove = (e: TouchEvent) => {
      e.preventDefault()
      applyScroll(touchStartPos + (e.touches[0].clientX - touchStartX) / 8)
    }

    containers.forEach(el => {
      el.addEventListener('wheel', wheelHandler, { passive: false })
      el.addEventListener('mousedown', mouseDown)
      el.addEventListener('touchstart', touchStart, { passive: true })
      el.addEventListener('touchmove', touchMove, { passive: false })
    })
    document.addEventListener('mousemove', mouseMove)
    document.addEventListener('mouseup', mouseUp)

    return () => {
      containers.forEach(el => {
        el.removeEventListener('wheel', wheelHandler)
        el.removeEventListener('mousedown', mouseDown)
        el.removeEventListener('touchstart', touchStart)
        el.removeEventListener('touchmove', touchMove)
      })
      document.removeEventListener('mousemove', mouseMove)
      document.removeEventListener('mouseup', mouseUp)
      if (rafId) cancelAnimationFrame(rafId)
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
              {priceTooltip.foreign != null && (
                <><span className="text-slate-500">外資</span><span style={{ color: priceTooltip.foreign >= 0 ? '#60a5fa' : '#f472b6' }}>{priceTooltip.foreign >= 0 ? '+' : ''}{fmt(priceTooltip.foreign)}</span></>
              )}
              {priceTooltip.trust != null && (
                <><span className="text-slate-500">投信</span><span style={{ color: priceTooltip.trust >= 0 ? '#34d399' : '#fb923c' }}>{priceTooltip.trust >= 0 ? '+' : ''}{fmt(priceTooltip.trust)}</span></>
              )}
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
          <div ref={foreignRef} className="w-full" />

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
