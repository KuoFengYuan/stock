'use client'
import { useState, useEffect, useCallback, useRef } from 'react'
import type { RecommendationItem } from '@/types/stock'

const SCROLL_KEY = 'home_scroll'

function useTimer(running: boolean) {
  const [elapsed, setElapsed] = useState(0)
  const startRef = useRef<number | null>(null)
  useEffect(() => {
    if (running) {
      startRef.current = Date.now()
      setElapsed(0)
      const id = setInterval(() => setElapsed(Math.floor((Date.now() - startRef.current!) / 1000)), 1000)
      return () => clearInterval(id)
    } else { startRef.current = null }
  }, [running])
  return elapsed
}

function fmtTime(sec: number) {
  if (sec < 60) return `${sec}秒`
  return `${Math.floor(sec / 60)}分${sec % 60}秒`
}

function timeAgo(ts: number | null) {
  if (!ts) return null
  const d = Math.floor((Date.now() - ts) / 1000)
  if (d < 60) return `${d}秒前`
  if (d < 3600) return `${Math.floor(d / 60)}分鐘前`
  if (d < 86400) return `${Math.floor(d / 3600)}小時前`
  return `${Math.floor(d / 86400)}天前`
}

type SyncStatus = Record<string, { lastSync: number | null; records: number | null }>
const PHASE: Record<string, string> = { prices: '同步價格', financials: '同步財報', chips: '同步籌碼', monthly_revenue: '同步月營收' }

export default function HomePage() {
  const [data, setData] = useState<{ date: string | null; total: number; items: RecommendationItem[] } | null>(null)
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)
  const [lastAnalyze, setLastAnalyze] = useState<string | null>(null)
  const [log, setLog] = useState('')
  const [progress, setProgress] = useState<{ phase: string; current: number; total: number } | null>(null)
  const [syncStatus, setSyncStatus] = useState<SyncStatus>({})
  const [showLog, setShowLog] = useState(false)
  const syncSec = useTimer(syncing)
  const analyzeSec = useTimer(analyzing)
  const progStart = useRef(0)

  const fetchStatus = useCallback(async () => {
    try { setSyncStatus(await (await fetch('/api/sync-status')).json()) } catch {}
  }, [])

  const fetchRec = useCallback(async () => {
    setLoading(true)
    try { setData(await (await fetch('/api/recommendations?limit=2000')).json()) } finally { setLoading(false) }
  }, [])

  useEffect(() => {
    fetchStatus()
    fetchRec().then(() => {
      const s = sessionStorage.getItem(SCROLL_KEY)
      if (s) { requestAnimationFrame(() => window.scrollTo(0, parseInt(s))); sessionStorage.removeItem(SCROLL_KEY) }
    })
  }, [fetchRec, fetchStatus])

  async function handleSync() {
    setSyncing(true); setLog(''); setProgress(null); progStart.current = Date.now()
    try {
      const res = await fetch('/api/sync', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'all' }) })
      if (!res.body) return
      const reader = res.body.getReader(), dec = new TextDecoder()
      let buf = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        const lines = buf.split('\n'); buf = lines.pop() ?? ''
        for (const line of lines) {
          if (!line.trim()) continue
          try {
            const msg = JSON.parse(line)
            if (msg.type === 'line') {
              const pm = (msg.text as string).match(/^@PROGRESS\|(\w+)\|(\d+)\|(\d+)$/)
              if (pm) {
                const c = parseInt(pm[2]); if (c === 0) progStart.current = Date.now()
                setProgress({ phase: pm[1], current: c, total: parseInt(pm[3]) })
              } else setLog(p => p + msg.text + '\n')
            } else if (msg.type === 'done') { setProgress(null); await fetchStatus(); await fetchRec() }
          } catch {}
        }
      }
    } finally { setSyncing(false) }
  }

  async function handleAnalyze(mode: 'rule' | 'ml') {
    setAnalyzing(true); setLog('')
    const t0 = Date.now()
    try {
      const j = await (await fetch('/api/analyze', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode }) })).json()
      setLog(j.output || j.error || '完成')
      setLastAnalyze(`${new Date().toLocaleTimeString('zh-TW')}（${fmtTime(Math.floor((Date.now() - t0) / 1000))}）`)
      await fetchRec()
    } finally { setAnalyzing(false) }
  }

  const pct = progress ? Math.round((progress.current / progress.total) * 100) : null
  const eta = progress && progress.current > 0 ? (() => {
    const r = progress.current / ((Date.now() - progStart.current) / 1000)
    const s = (progress.total - progress.current) / r
    return s > 0 ? fmtTime(Math.ceil(s)) : null
  })() : null
  const busy = syncing || analyzing
  const lastSync = Math.max(...(['prices', 'chips', 'financials', 'monthly_revenue'] as const).map(k => syncStatus[k]?.lastSync ?? 0))

  return (
    <div className="max-w-[1600px] mx-auto">
      {/* 頂部 */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 mb-3">
        <div>
          <h1 className="text-lg font-bold text-white">今日推薦</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            {data?.date && <>{data.date} · {data.items.length} 檔</>}
            {lastSync > 0 && <> · 同步{timeAgo(lastSync)}</>}
            {lastAnalyze && <> · 分析{lastAnalyze}</>}
          </p>
        </div>
        <div className="flex gap-2 flex-wrap">
          <button onClick={handleSync} disabled={busy}
            className="h-8 px-3 sm:px-4 bg-slate-700 hover:bg-slate-600 disabled:opacity-50 text-white text-sm rounded-md transition-colors inline-flex items-center gap-1.5">
            {syncing && <span className="animate-spin w-3 h-3 border-2 border-white/30 border-t-white rounded-full" />}
            {syncing ? `同步中 ${fmtTime(syncSec)}` : '同步資料'}
          </button>
          <button onClick={() => handleAnalyze('ml')} disabled={busy}
            className="h-8 px-3 sm:px-4 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm rounded-md transition-colors font-medium">
            {analyzing ? `分析中 ${fmtTime(analyzeSec)}` : 'AI 分析'}
          </button>
        </div>
      </div>

      {/* 進度條 */}
      {syncing && (
        <div className="mb-2">
          <div className="flex justify-between text-xs text-slate-400 mb-0.5">
            <span>{PHASE[progress?.phase ?? ''] || '初始化'}…</span>
            <span>{pct !== null && `${pct}%`}{eta && ` · 剩 ${eta}`}</span>
          </div>
          <div className="w-full h-1 bg-slate-800 rounded-full overflow-hidden">
            <div className={`h-full rounded-full transition-all duration-300 ${pct !== null ? 'bg-blue-500' : 'bg-blue-500/50 animate-pulse'}`}
              style={{ width: pct !== null ? `${pct}%` : '100%' }} />
          </div>
        </div>
      )}

      {log && (
        <div className="mb-2">
          <button onClick={() => setShowLog(v => !v)} className="text-xs text-slate-500 hover:text-slate-300">
            {showLog ? '▾ 隱藏日誌' : '▸ 日誌'}
          </button>
          {showLog && (
            <pre className="mt-1 p-2 bg-slate-800/80 text-slate-400 text-xs rounded overflow-auto max-h-28 whitespace-pre-wrap">
              {log.split('\n').filter(l => !/[^\x00-\x7F\u4e00-\u9fff\u3400-\u4dbf\u2000-\u206f\uff00-\uffef\s]/.test(l) || l.trim() === '').join('\n')}
            </pre>
          )}
        </div>
      )}

      {loading ? <Skeleton /> : !data?.items.length ? (
        <div className="text-slate-500 text-center py-16 text-sm">尚無推薦資料，請先同步資料再執行分析</div>
      ) : <Table items={data.items} />}
    </div>
  )
}

function Skeleton() {
  return <div className="animate-pulse space-y-1.5 mt-4">{Array.from({ length: 10 }).map((_, i) => <div key={i} className="h-9 bg-slate-800/50 rounded" />)}</div>
}

// ────────────────────────────────────────
type SK = 'score' | 'changePct' | 'volume' | 'close'

type PerfSummary = {
  n5: number; n20: number
  avg5: number | null; avg20: number | null
  excess5: number | null; excess20: number | null  // vs 大盤超額
  net5: number | null; net20: number | null        // 扣 0.585% 交易成本
  beat5: number | null; beat20: number | null      // 勝過大盤比例
}
type Perf = {
  days: number; total: number; tx_cost: number
  by_signal: Record<string, PerfSummary>
  by_model: Record<string, PerfSummary>
}

function PerfHeader() {
  const [perf, setPerf] = useState<Perf | null>(null)
  const [open, setOpen] = useState(false)
  useEffect(() => {
    fetch('/api/performance?days=90').then(r => r.json()).then(setPerf).catch(() => {})
  }, [])
  if (!perf) return null
  const buy = perf.by_signal?.buy
  const fmt = (v: number | null, pct = false) =>
    v == null ? '-' : pct ? `${(v * 100).toFixed(0)}%` : `${v >= 0 ? '+' : ''}${v.toFixed(1)}%`
  const col = (v: number | null) =>
    v == null ? 'text-slate-500' : v >= 0 ? 'text-red-400' : 'text-green-400'
  // 選最佳可用時窗：20d 優先（需 ≥10 樣本），否則 5d
  const best = buy && buy.n20 >= 10 ? { k: '20', n: buy.n20, excess: buy.excess20, net: buy.net20, beat: buy.beat20, avg: buy.avg20 }
             : buy && buy.n5 >= 10 ? { k: '5', n: buy.n5, excess: buy.excess5, net: buy.net5, beat: buy.beat5, avg: buy.avg5 }
             : null

  return (
    <div className="bg-slate-800/40 rounded-md p-2 mb-2 text-xs">
      <button onClick={() => setOpen(!open)} className="flex items-center gap-3 w-full text-left">
        <span className="text-slate-400">📊 近 {perf.days} 天 Buy vs 大盤</span>
        {best ? (
          <>
            <span className="text-slate-500">|</span>
            <span title="buy 平均報酬 減 當日全市場平均報酬">
              {best.k}日超額 <span className={`font-mono font-semibold ${col(best.excess)}`}>{fmt(best.excess)}</span>
            </span>
            <span className="text-slate-500">|</span>
            <span title="扣一買一賣 0.585% 成本">
              扣成本 <span className={`font-mono ${col(best.net)}`}>{fmt(best.net)}</span>
            </span>
            <span className="text-slate-500">|</span>
            <span title="buy 報酬 > 大盤報酬 的比例">
              勝大盤 <span className="font-mono text-slate-300">{fmt(best.beat, true)}</span>
            </span>
            <span className="text-slate-600 text-[10px]">({best.n} 樣本)</span>
          </>
        ) : (
          <span className="text-slate-500 text-[11px]">資料累積中（需要 5 個交易日後才能評估）</span>
        )}
        <span className="text-slate-500 ml-auto">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="mt-2 pt-2 border-t border-slate-700/50 space-y-2">
          {/* Buy 5d / 20d 詳情 */}
          <div className="grid grid-cols-2 gap-2">
            {(['5', '20'] as const).map(k => {
              const n = buy?.[`n${k}` as 'n5'] ?? 0
              const avg = buy?.[`avg${k}` as 'avg5'] ?? null
              const ex = buy?.[`excess${k}` as 'excess5'] ?? null
              const net = buy?.[`net${k}` as 'net5'] ?? null
              const beat = buy?.[`beat${k}` as 'beat5'] ?? null
              return (
                <div key={k} className="bg-slate-900/40 rounded p-2">
                  <div className="text-slate-400 mb-1">持有 {k} 個交易日（{n} 筆樣本）</div>
                  <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-[10px]">
                    <span className="text-slate-500">原始報酬</span>
                    <span className={`font-mono text-right ${col(avg)}`}>{fmt(avg)}</span>
                    <span className="text-slate-500">vs 大盤超額</span>
                    <span className={`font-mono text-right font-semibold ${col(ex)}`}>{fmt(ex)}</span>
                    <span className="text-slate-500">扣成本淨利</span>
                    <span className={`font-mono text-right ${col(net)}`}>{fmt(net)}</span>
                    <span className="text-slate-500">勝大盤比例</span>
                    <span className="font-mono text-right text-slate-300">{fmt(beat, true)}</span>
                  </div>
                </div>
              )
            })}
          </div>
          {/* 4 模型 Top 20 組合超額報酬 */}
          <div>
            <div className="text-slate-400 text-[10px] mb-1">各派 Top20 組合（20 日超額報酬，不足 5 日資料顯示）</div>
            <div className="grid grid-cols-4 gap-1">
              {(['main', 'breakout', 'value', 'chip'] as const).map(m => {
                const p = perf.by_model?.[m]
                const label = { main: '綜合', breakout: '動能派', value: '價值派', chip: '跟主力' }[m]
                const ex = p && p.n20 >= 10 ? p.excess20 : p?.excess5 ?? null
                const k = p && p.n20 >= 10 ? '20' : '5'
                const n = p && p.n20 >= 10 ? p.n20 : p?.n5 ?? 0
                return (
                  <div key={m} className="bg-slate-900/40 rounded p-1.5">
                    <div className="text-[10px] text-slate-500">{label} ({k}日)</div>
                    <div className={`font-mono text-[11px] font-semibold ${col(ex)}`}>{fmt(ex)}</div>
                    <div className="text-[9px] text-slate-600">{n} 樣本</div>
                  </div>
                )
              })}
            </div>
          </div>
          <div className="text-[10px] text-slate-600 leading-relaxed">
            <b>vs 大盤超額</b>：推薦平均報酬 − 當日全市場平均報酬（正值代表跑贏大盤）。<br />
            <b>扣成本淨利</b>：扣除一買一賣 {(perf.tx_cost || 0.585).toFixed(3)}%（手續費 0.1425%×2 + 證交稅 0.3%）。<br />
            <b>勝大盤比例</b>：推薦股票報酬超過當日大盤的比例（&gt; 50% 才算有 alpha）。
          </div>
        </div>
      )}
    </div>
  )
}

function Table({ items }: { items: RecommendationItem[] }) {
  const [sig, setSig] = useState<'all' | 'buy' | 'watch' | 'neutral'>('all')
  const [tag, setTag] = useState<string | null>(null)
  const [sk, setSk] = useState<SK>('score')
  const [asc, setAsc] = useState(false)
  // 5 維度最低分篩選（default: 全部關閉）
  const [minFund, setMinFund] = useState(0)
  const [minMom, setMinMom] = useState(0)
  const [minChip, setMinChip] = useState(0)
  const [minVal, setMinVal] = useState(0)
  const [minVolume, setMinVolume] = useState(3000) // 成交量門檻
  const [topN, setTopN] = useState<number | null>(null) // 只看 top N
  // 預設風格排序：啟動時依對應 ml_sub_scores 排序（覆寫 sk）
  const [presetSort, setPresetSort] = useState<'breakout' | 'value' | 'chip' | null>(null)

  function sort(k: SK) { setPresetSort(null); k === sk ? setAsc(!asc) : (setSk(k), setAsc(false)) }

  const sorted = [...items].sort((a, b) => {
    const ai = (a.tags?.some(t => t.tag === 'AI') ? 1 : 0) - (b.tags?.some(t => t.tag === 'AI') ? 1 : 0)
    if (ai) return -ai
    // 預設排序：混合 ML 模型分數 + 規則維度分數（雙保險，降低單一模型 bias）
    // 權重依各 ML 模型 AUC 品質反向調整：ML 越不可靠（AUC 低），規則權重越重
    if (presetSort) {
      const mix = (mlKey: 'breakout' | 'value' | 'chip', dimKey: 'momentum' | 'valuation' | 'chip', mlW: number) => {
        const getScore = (it: RecommendationItem) => {
          const ml = it.mlSubScores?.[mlKey] ?? it.score
          const rule = (it.dimScores?.[dimKey] ?? 50) / 100
          return ml * mlW + rule * (1 - mlW)
        }
        return [getScore(a), getScore(b)]
      }
      let av: number, bv: number
      if (presetSort === 'breakout') {
        // breakout AUC 0.614 （較高） → ML 0.55
        [av, bv] = mix('breakout', 'momentum', 0.55)
      } else if (presetSort === 'value') {
        // value AUC 0.590 → ML 0.50
        [av, bv] = mix('value', 'valuation', 0.50)
      } else if (presetSort === 'chip') {
        // chip AUC 0.566 （最低，易過擬合短期動能） → ML 0.40，規則 0.60
        [av, bv] = mix('chip', 'chip', 0.40)
      } else {
        av = a.score; bv = b.score
      }
      return bv - av
    }
    return asc ? ((a[sk] ?? 0) as number) - ((b[sk] ?? 0) as number) : ((b[sk] ?? 0) as number) - ((a[sk] ?? 0) as number)
  })

  // 套用維度篩選
  const dimFiltered = sorted.filter(i => {
    if (!i.dimScores) return minFund === 0 && minMom === 0 && minChip === 0 && minVal === 0
    const d = i.dimScores
    return d.fundamental >= minFund && d.momentum >= minMom && d.chip >= minChip && d.valuation >= minVal
  })

  // 成交量篩選
  const volFiltered = dimFiltered.filter(i => (i.volume ?? 0) >= minVolume)

  const bySig = sig === 'all' ? volFiltered : volFiltered.filter(i => i.signal === sig)

  // AI sub-tags
  const stc = new Map<string, number>()
  for (const it of bySig) for (const s of new Set(it.tags?.filter(t => t.tag === 'AI' && t.sub_tag).map(t => t.sub_tag!) ?? [])) stc.set(s, (stc.get(s) ?? 0) + 1)
  const subs = [...stc.entries()].filter(([, c]) => c > 0).sort((a, b) => b[1] - a[1]).map(([s]) => s)

  const tagFiltered = tag ? bySig.filter(i => i.tags?.some(t => t.tag === 'AI' && t.sub_tag === tag)) : bySig
  const rows = topN ? tagFiltered.slice(0, topN) : tagFiltered

  const cnt = { all: items.length, buy: items.filter(i => i.signal === 'buy').length, watch: items.filter(i => i.signal === 'watch').length, neutral: items.filter(i => i.signal === 'neutral').length }

  return (
    <div>
      <PerfHeader />
      {/* 篩選列 */}
      <div className="flex items-center gap-4 mb-2">
        <div className="flex gap-1.5">
          {([
            ['all', `全部 ${cnt.all}`, 'bg-slate-600 text-white'],
            ['buy', `買入 ${cnt.buy}`, 'bg-green-900/60 text-green-300 border border-green-700/80'],
            ['watch', `觀察 ${cnt.watch}`, 'bg-yellow-900/60 text-yellow-300 border border-yellow-700/80'],
            ['neutral', `中立 ${cnt.neutral}`, 'bg-slate-700 text-slate-400'],
          ] as const).map(([k, label, cls]) => (
            <button key={k} onClick={() => setSig(k)} className={`text-xs px-2.5 py-1 rounded-md transition-all ${sig === k ? cls : 'bg-slate-800/60 text-slate-500 hover:text-slate-300'}`}>
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* AI 主題篩選 */}
      {subs.length > 0 && (
        <div className="flex items-center gap-2 mb-2">
          <span className="text-xs text-slate-500">AI 主題</span>
          <select
            value={tag ?? ''}
            onChange={e => setTag(e.target.value || null)}
            className="text-xs bg-slate-800 text-slate-300 border border-slate-700 rounded-md px-2 py-1 focus:outline-none focus:border-violet-500"
          >
            <option value="">全部 ({bySig.filter(i => i.tags?.some(t => t.tag === 'AI')).length})</option>
            {subs.map(s => (
              <option key={s} value={s}>{s} ({stc.get(s)})</option>
            ))}
          </select>
        </div>
      )}

      {/* 投資風格預設 + 維度篩選 */}
      <div className="bg-slate-800/40 rounded-md p-2 mb-2 space-y-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-slate-500 mr-1">預設：</span>
          <button onClick={() => { setMinFund(0); setMinMom(0); setMinChip(0); setMinVal(0); setTopN(null); setPresetSort(null) }}
            className="text-[11px] px-2 py-0.5 rounded bg-slate-700 hover:bg-slate-600 text-slate-300">清除篩選</button>
          <button onClick={() => { setMinMom(70); setMinChip(60); setMinFund(50); setMinVal(0); setTopN(20); setPresetSort('breakout') }}
            className={`text-[11px] px-2 py-0.5 rounded border ${presetSort==='breakout' ? 'bg-green-900 text-green-200 border-green-500' : 'bg-green-900/60 hover:bg-green-900 text-green-300 border-green-700/60'}`}>⚡ 動能派 (Top 20)</button>
          <button onClick={() => { setMinFund(70); setMinVal(60); setMinMom(0); setMinChip(0); setTopN(20); setPresetSort('value') }}
            className={`text-[11px] px-2 py-0.5 rounded border ${presetSort==='value' ? 'bg-blue-900 text-blue-200 border-blue-500' : 'bg-blue-900/60 hover:bg-blue-900 text-blue-300 border-blue-700/60'}`}>💎 價值派 (Top 20)</button>
          <button onClick={() => { setMinFund(60); setMinMom(60); setMinChip(60); setMinVal(0); setTopN(20); setPresetSort(null) }}
            className={`text-[11px] px-2 py-0.5 rounded border ${presetSort===null && minFund===60 && minMom===60 && minChip===60 ? 'bg-violet-900 text-violet-200 border-violet-500' : 'bg-violet-900/60 hover:bg-violet-900 text-violet-300 border-violet-700/60'}`}>⚖️ 均衡派 (Top 20)</button>
          <button onClick={() => { setMinFund(0); setMinMom(0); setMinChip(80); setMinVal(0); setTopN(30); setPresetSort('chip') }}
            className={`text-[11px] px-2 py-0.5 rounded border ${presetSort==='chip' ? 'bg-orange-900 text-orange-200 border-orange-500' : 'bg-orange-900/60 hover:bg-orange-900 text-orange-300 border-orange-700/60'}`}>🏛 跟主力 (Top 30)</button>
        </div>
        <div className="flex flex-wrap items-center gap-3 text-[11px] text-slate-400">
          <DimSlider label="基本面" value={minFund} onChange={setMinFund} color="emerald" />
          <DimSlider label="動能" value={minMom} onChange={setMinMom} color="red" />
          <DimSlider label="籌碼" value={minChip} onChange={setMinChip} color="blue" />
          <DimSlider label="估值" value={minVal} onChange={setMinVal} color="yellow" />
          <label className="flex items-center gap-1.5">
            <span>成交量≥</span>
            <input type="number" value={minVolume} onChange={e => setMinVolume(parseInt(e.target.value) || 0)}
              className="w-16 px-1.5 py-0.5 bg-slate-800 border border-slate-700 rounded text-slate-300" />
            <span>張</span>
          </label>
          <label className="flex items-center gap-1.5">
            <span>Top</span>
            <select value={topN ?? ''} onChange={e => setTopN(e.target.value ? parseInt(e.target.value) : null)}
              className="px-1.5 py-0.5 bg-slate-800 border border-slate-700 rounded text-slate-300">
              <option value="">全部</option>
              <option value="10">10</option>
              <option value="20">20</option>
              <option value="30">30</option>
              <option value="50">50</option>
            </select>
          </label>
          <span className="text-slate-500 ml-auto">過濾後：{rows.length} 檔</span>
        </div>
      </div>

      {/* 手機版卡片 */}
      <div className="sm:hidden space-y-1">
        {rows.map(it => <MobileCard key={it.symbol} item={it} />)}
      </div>

      {/* 桌面表格 */}
      <div className="hidden sm:block overflow-x-auto">
        <table className="w-full" style={{ minWidth: 900 }}>
          <thead>
            <tr className="text-xs text-slate-500 border-b border-slate-700/50">
              <th className="text-left py-2 px-3 font-medium">股票</th>
              <Th label="收盤" k="close" sk={sk} asc={asc} sort={sort} />
              <Th label="漲跌" k="changePct" sk={sk} asc={asc} sort={sort} />
              <Th label="成交量" k="volume" sk={sk} asc={asc} sort={sort} />
              <Th label="評分" k="score" sk={sk} asc={asc} sort={sort} />
              <th className="text-right py-2 px-3 font-medium whitespace-nowrap" title="本益比 PE = 股價 / EPS TTM（顏色依 PEG 分級，有 PEG 資料時更準）">PE</th>
              <th className="text-right py-2 px-3 font-medium whitespace-nowrap" title="PEG = PE / 獲利年增率 YoY%（&lt;1 偏低估、1-2 合理、&gt;2 偏貴）">PEG</th>
              <th className="text-center py-2 px-3 font-medium whitespace-nowrap" title="ML 四模型看多機率：綜合排名 / 動能突破 / 價值估值 / 主力籌碼">AI模型</th>
              <th className="text-center py-2 px-3 font-medium whitespace-nowrap">訊號</th>
              <th className="text-center py-2 px-3 font-medium whitespace-nowrap" title="7 位投資大師（Buffett / Graham / Munger / Fisher / Druckenmiller / Wood / Ackman）共識">大師</th>
              <th className="text-left py-2 px-3 font-medium">推薦理由</th>
            </tr>
          </thead>
          <tbody className="text-sm">
            {rows.map(it => (
              <tr key={it.symbol} className="border-b border-slate-800/30 hover:bg-slate-800/40 transition-colors group">
                <td className="py-2 px-3">
                  <a href={`/stocks/${it.symbol}`} onClick={() => sessionStorage.setItem(SCROLL_KEY, String(window.scrollY))} className="text-blue-400 hover:text-blue-300 font-medium">
                    {it.symbol.replace('.TW', '').replace('.TWO', '')}
                  </a>
                  <span className="text-slate-500 text-xs ml-1.5">{it.name}</span>
                  {it.tags?.some(t => t.tag === 'AI') && <span className="text-[10px] px-1 ml-1 rounded bg-violet-900/60 text-violet-300 align-middle">AI</span>}
                  {it.tags?.some(t => t.tag === 'AI') && (
                    <div className="mt-0.5">
                      {[...new Set(it.tags.filter(t => t.tag === 'AI' && t.sub_tag).map(t => t.sub_tag))].map((s, i) => (
                        <span key={i} className="text-[10px] px-1 mr-0.5 rounded bg-slate-800 text-violet-400/70">{s}</span>
                      ))}
                    </div>
                  )}
                </td>
                <td className="py-2 px-3 text-right font-mono tabular-nums whitespace-nowrap">{it.close?.toFixed(2) ?? '-'}</td>
                <td className={`py-2 px-3 text-right font-mono tabular-nums whitespace-nowrap ${it.changePct == null ? 'text-slate-600' : it.changePct >= 0 ? 'text-red-400' : 'text-green-400'}`}>
                  {it.changePct != null ? `${it.changePct >= 0 ? '+' : ''}${it.changePct.toFixed(2)}%` : '-'}
                </td>
                <td className="py-2 px-3 text-right font-mono tabular-nums text-slate-400 whitespace-nowrap">{it.volume ? it.volume.toLocaleString() : '-'}</td>
                <td className="py-2 px-3 text-right whitespace-nowrap"><Score v={it.score} /></td>
                <td className="py-2 px-3 text-right font-mono tabular-nums whitespace-nowrap"><PeCell pe={it.peRatio} peg={it.pegRatio} /></td>
                <td className="py-2 px-3 text-right font-mono tabular-nums whitespace-nowrap"><PegCell peg={it.pegRatio} /></td>
                <td className="py-2 px-3 text-center whitespace-nowrap"><MlBars m={it.mlSubScores} /></td>
                <td className="py-2 px-3 text-center whitespace-nowrap"><Signal s={it.signal} /></td>
                <td className="py-2 px-3 text-center whitespace-nowrap"><Consensus c={it.agentConsensus} d={it.agentDetails} /></td>
                <td className="py-2 px-3">
                  <div className="flex flex-wrap gap-1">
                    {it.reasons.map((r, i) => <Tag key={i} r={r} />)}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="text-xs text-slate-600 text-right mt-1">{rows.length} 檔</p>
      </div>
    </div>
  )
}

function MobileCard({ item: it }: { item: RecommendationItem }) {
  return (
    <a href={`/stocks/${it.symbol}`} onClick={() => sessionStorage.setItem(SCROLL_KEY, String(window.scrollY))}
      className="flex flex-col gap-1.5 bg-slate-800/40 rounded-lg px-3 py-2.5 active:bg-slate-700/40">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <span className="text-blue-400 font-semibold">{it.symbol.replace('.TW', '').replace('.TWO', '')}</span>
          <span className="text-slate-500 text-xs">{it.name}</span>
          {it.tags?.some(t => t.tag === 'AI') && <span className="text-[10px] px-1 rounded bg-violet-900/60 text-violet-300">AI</span>}
        </div>
        <div className="flex items-baseline gap-2">
          <span className="font-mono text-white">{it.close?.toFixed(2) ?? '-'}</span>
          <span className={`text-xs font-mono ${it.changePct == null ? 'text-slate-600' : it.changePct >= 0 ? 'text-red-400' : 'text-green-400'}`}>
            {it.changePct != null ? `${it.changePct >= 0 ? '+' : ''}${it.changePct.toFixed(2)}%` : ''}
          </span>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <Signal s={it.signal} />
        <Score v={it.score} />
        <Consensus c={it.agentConsensus} d={it.agentDetails} />
        <MlBars m={it.mlSubScores} />
        <span className="text-xs text-slate-600 ml-auto">{it.volume ? it.volume.toLocaleString() + '張' : ''}</span>
      </div>
      {it.reasons.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {it.reasons.slice(0, 4).map((r, i) => <Tag key={i} r={r} />)}
          {it.reasons.length > 4 && <span className="text-[10px] text-slate-600 self-center">+{it.reasons.length - 4}</span>}
        </div>
      )}
    </a>
  )
}

function Th({ label, k, sk, asc, sort }: { label: string; k: SK; sk: SK; asc: boolean; sort: (k: SK) => void }) {
  return (
    <th className="text-right py-2 px-3 font-medium cursor-pointer select-none hover:text-slate-300 transition-colors whitespace-nowrap text-xs"
      onClick={() => sort(k)}>
      {label}{sk === k && <span className="text-blue-400 ml-0.5 text-[10px]">{asc ? '▲' : '▼'}</span>}
    </th>
  )
}

function Tag({ r }: { r: string }) {
  const w = r.startsWith('⚠'), d = r.includes('買超／') || r.includes('賣超／')
  return (
    <span className={`text-[11px] px-1.5 py-0.5 rounded leading-tight ${
      w ? 'bg-red-950/60 text-red-300 border border-red-800/50'
      : d ? 'bg-amber-950/50 text-amber-300 border border-amber-700/50'
      : 'bg-slate-800/80 text-slate-300'
    }`}>{r}</span>
  )
}

function Score({ v }: { v: number }) {
  const p = Math.round(v * 100)
  const c = p >= 70 ? 'bg-green-500' : p >= 50 ? 'bg-yellow-500' : 'bg-slate-600'
  return (
    <div className="inline-flex items-center gap-1.5">
      <div className="w-14 h-1.5 bg-slate-700/50 rounded-full overflow-hidden">
        <div className={`h-full ${c} rounded-full`} style={{ width: `${p}%` }} />
      </div>
      <span className="text-xs text-slate-300 font-mono w-6 text-right">{p}</span>
    </div>
  )
}

function Signal({ s }: { s: string }) {
  const m: Record<string, string> = {
    buy: 'bg-green-900/60 text-green-300 border border-green-700/60',
    watch: 'bg-yellow-900/60 text-yellow-300 border border-yellow-700/60',
    neutral: 'bg-slate-700/60 text-slate-500',
  }
  const l: Record<string, string> = { buy: '買入', watch: '觀察', neutral: '中立' }
  return <span className={`text-xs px-2 py-0.5 rounded ${m[s] ?? m.neutral}`}>{l[s] ?? s}</span>
}

function DimSlider({ label, value, onChange, color }: {
  label: string; value: number; onChange: (v: number) => void; color: 'emerald' | 'red' | 'blue' | 'yellow'
}) {
  const colorMap: Record<string, string> = {
    emerald: 'accent-emerald-500',
    red: 'accent-red-500',
    blue: 'accent-blue-500',
    yellow: 'accent-yellow-500',
  }
  return (
    <label className="flex items-center gap-1.5">
      <span className="w-12">{label}≥</span>
      <input type="range" min={0} max={100} step={5} value={value} onChange={e => onChange(parseInt(e.target.value))}
        className={`w-20 ${colorMap[color]}`} />
      <span className="w-6 text-right font-mono">{value}</span>
    </label>
  )
}

function PeCell({ pe, peg }: { pe?: number | null; peg?: number | null }) {
  if (pe == null) return <span className="text-slate-700">-</span>
  // 顏色依 PEG 分級（若有）；沒 PEG 時依 PE 絕對值
  let cls = 'text-slate-300'
  if (peg != null) {
    if (peg < 1) cls = 'text-emerald-400'          // 低估（成長股）
    else if (peg < 2) cls = 'text-slate-300'        // 合理
    else cls = 'text-red-400'                       // 偏貴
  } else {
    if (pe < 15) cls = 'text-emerald-400'
    else if (pe < 30) cls = 'text-slate-300'
    else if (pe < 60) cls = 'text-amber-400'
    else cls = 'text-red-400'
  }
  return <span className={cls}>{pe.toFixed(1)}</span>
}

function PegCell({ peg }: { peg?: number | null }) {
  if (peg == null) return <span className="text-slate-700">-</span>
  const cls = peg < 1 ? 'text-emerald-400' : peg < 2 ? 'text-slate-300' : 'text-red-400'
  return <span className={cls} title={peg < 1 ? 'PEG < 1 偏低估' : peg < 2 ? 'PEG 1-2 合理' : 'PEG > 2 偏貴'}>{peg.toFixed(2)}</span>
}

function MlBars({ m }: { m?: { main: number; breakout: number; value: number; chip: number } | null }) {
  if (!m) return <span className="text-slate-700 text-xs">-</span>
  // 4 個子模型機率 0~1 → 視覺化成 bar（0.5 以上才算看多）
  const cells: Array<{ label: string; value: number; color: string }> = [
    { label: '綜', value: m.main, color: '#a855f7' },       // 主 ranker (purple)
    { label: '動', value: m.breakout, color: '#22c55e' },    // breakout (green)
    { label: '值', value: m.value, color: '#3b82f6' },       // value (blue)
    { label: '籌', value: m.chip, color: '#f97316' },        // chip (orange)
  ]
  const tip = `main ${(m.main*100).toFixed(0)}% / breakout ${(m.breakout*100).toFixed(0)}% / value ${(m.value*100).toFixed(0)}% / chip ${(m.chip*100).toFixed(0)}%`
  return (
    <div className="inline-flex items-center gap-0.5" title={tip}>
      {cells.map(c => (
        <div key={c.label} className="flex flex-col items-center">
          <div className="w-3 h-6 bg-slate-800 rounded-sm overflow-hidden flex items-end">
            <div style={{ height: `${Math.round(c.value * 100)}%`, background: c.color, width: '100%' }} />
          </div>
          <span className="text-[8px] text-slate-500 leading-none mt-0.5">{c.label}</span>
        </div>
      ))}
    </div>
  )
}

function DimBars({ d }: { d?: { fundamental: number; momentum: number; chip: number; valuation: number; consensus: number } | null }) {
  if (!d) return <span className="text-slate-700 text-xs">-</span>
  const cells: Array<{ label: string; value: number; color: string }> = [
    { label: '基', value: d.fundamental, color: '#10b981' },
    { label: '動', value: d.momentum, color: '#ef4444' },
    { label: '籌', value: d.chip, color: '#3b82f6' },
    { label: '估', value: d.valuation, color: '#eab308' },
  ]
  return (
    <div className="inline-flex items-center gap-0.5" title={`基本面 ${d.fundamental} / 動能 ${d.momentum} / 籌碼 ${d.chip} / 估值 ${d.valuation} / 大師 ${d.consensus}/7`}>
      {cells.map(c => (
        <div key={c.label} className="flex flex-col items-center">
          <div className="w-3 h-6 bg-slate-800 rounded-sm overflow-hidden flex items-end">
            <div style={{ height: `${c.value}%`, background: c.color, width: '100%' }} />
          </div>
          <span className="text-[8px] text-slate-500 leading-none mt-0.5">{c.label}</span>
        </div>
      ))}
    </div>
  )
}

function Consensus({ c, d }: {
  c?: { bullish: number; neutral: number; bearish: number } | null
  d?: Array<{ name: string; signal: string; confidence: number; reasons: string[] }> | null
}) {
  if (!c) return <span className="text-slate-700 text-xs">-</span>
  const total = c.bullish + c.neutral + c.bearish
  const tooltip = d ? d.map(a => `${a.name}: ${a.signal === 'bullish' ? '看多' : a.signal === 'bearish' ? '看空' : '中立'}${a.reasons.length ? ' — ' + a.reasons.slice(0, 2).join('、') : ''}`).join('\n') : undefined
  const cls = c.bullish >= 5 ? 'bg-green-900/60 text-green-300 border border-green-700/60'
    : c.bearish >= 5 ? 'bg-red-900/60 text-red-300 border border-red-700/60'
    : c.bullish >= 3 ? 'bg-green-900/30 text-green-400/80'
    : c.bearish >= 3 ? 'bg-red-900/30 text-red-400/80'
    : 'bg-slate-700/50 text-slate-400'
  return (
    <span className={`inline-block text-[11px] px-1.5 py-0.5 rounded font-mono cursor-help ${cls}`} title={tooltip}>
      {c.bullish}/{total}
    </span>
  )
}
