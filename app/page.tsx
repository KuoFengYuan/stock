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
      <div className="flex items-center justify-between mb-3">
        <div>
          <h1 className="text-lg font-bold text-white">今日推薦</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            {data?.date && <>{data.date} · {data.items.length} 檔</>}
            {lastSync > 0 && <> · 同步{timeAgo(lastSync)}</>}
            {lastAnalyze && <> · 分析{lastAnalyze}</>}
          </p>
        </div>
        <div className="flex gap-2">
          <button onClick={handleSync} disabled={busy}
            className="h-8 px-4 bg-slate-700 hover:bg-slate-600 disabled:opacity-50 text-white text-sm rounded-md transition-colors inline-flex items-center gap-1.5">
            {syncing && <span className="animate-spin w-3 h-3 border-2 border-white/30 border-t-white rounded-full" />}
            {syncing ? `同步中 ${fmtTime(syncSec)}` : '同步資料'}
          </button>
          <button onClick={() => handleAnalyze('rule')} disabled={busy}
            className="h-8 px-4 bg-slate-700 hover:bg-slate-600 disabled:opacity-50 text-white text-sm rounded-md transition-colors">
            {analyzing ? `分析中 ${fmtTime(analyzeSec)}` : '規則分析'}
          </button>
          <button onClick={() => handleAnalyze('ml')} disabled={busy}
            className="h-8 px-4 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm rounded-md transition-colors font-medium">
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

function Table({ items }: { items: RecommendationItem[] }) {
  const [sig, setSig] = useState<'all' | 'buy' | 'watch' | 'neutral'>('all')
  const [tag, setTag] = useState<string | null>(null)
  const [sk, setSk] = useState<SK>('score')
  const [asc, setAsc] = useState(false)

  function sort(k: SK) { k === sk ? setAsc(!asc) : (setSk(k), setAsc(false)) }

  const sorted = [...items].sort((a, b) => {
    const ai = (a.tags?.some(t => t.tag === 'AI') ? 1 : 0) - (b.tags?.some(t => t.tag === 'AI') ? 1 : 0)
    if (ai) return -ai
    return asc ? ((a[sk] ?? 0) as number) - ((b[sk] ?? 0) as number) : ((b[sk] ?? 0) as number) - ((a[sk] ?? 0) as number)
  })

  const bySig = sig === 'all' ? sorted : sorted.filter(i => i.signal === sig)

  // AI sub-tags
  const stc = new Map<string, number>()
  for (const it of bySig) for (const s of new Set(it.tags?.filter(t => t.tag === 'AI' && t.sub_tag).map(t => t.sub_tag!) ?? [])) stc.set(s, (stc.get(s) ?? 0) + 1)
  const subs = [...stc.entries()].filter(([, c]) => c > 0).sort((a, b) => b[1] - a[1]).map(([s]) => s)

  const rows = tag ? bySig.filter(i => i.tags?.some(t => t.tag === 'AI' && t.sub_tag === tag)) : bySig

  const cnt = { all: items.length, buy: items.filter(i => i.signal === 'buy').length, watch: items.filter(i => i.signal === 'watch').length, neutral: items.filter(i => i.signal === 'neutral').length }

  return (
    <div>
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
              <th className="text-center py-2 px-3 font-medium whitespace-nowrap">訊號</th>
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
                <td className="py-2 px-3 text-center whitespace-nowrap"><Signal s={it.signal} /></td>
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
