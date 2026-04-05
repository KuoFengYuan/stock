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
      const id = setInterval(() => {
        setElapsed(Math.floor((Date.now() - startRef.current!) / 1000))
      }, 1000)
      return () => clearInterval(id)
    } else {
      startRef.current = null
    }
  }, [running])

  return elapsed
}

function formatElapsed(sec: number) {
  if (sec < 60) return `${sec} 秒`
  return `${Math.floor(sec / 60)} 分 ${sec % 60} 秒`
}

export default function HomePage() {
  const [data, setData] = useState<{ date: string | null; total: number; items: RecommendationItem[] } | null>(null)
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)
  const [lastSyncTime, setLastSyncTime] = useState<string | null>(null)
  const [lastAnalyzeTime, setLastAnalyzeTime] = useState<string | null>(null)
  const [log, setLog] = useState('')
  const [syncProgress, setSyncProgress] = useState<number | null>(null)
  const [syncPhase, setSyncPhase] = useState('')

  const syncElapsed = useTimer(syncing)
  const analyzeElapsed = useTimer(analyzing)

  const fetchRecommendations = useCallback(async () => {
    setLoading(true)
    try {
      const res = await fetch('/api/recommendations?limit=2000')
      const json = await res.json()
      setData(json)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchRecommendations().then(() => {
      const saved = sessionStorage.getItem(SCROLL_KEY)
      if (saved) {
        requestAnimationFrame(() => { window.scrollTo(0, parseInt(saved)) })
        sessionStorage.removeItem(SCROLL_KEY)
      }
    })
  }, [fetchRecommendations])

  async function handleSync() {
    setSyncing(true)
    setLog('')
    setSyncProgress(null)
    setSyncPhase('初始化...')
    const start = Date.now()
    try {
      const res = await fetch('/api/sync', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'all' }) })
      if (!res.body) return
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const lines = buf.split('\n')
        buf = lines.pop() ?? ''
        for (const line of lines) {
          if (!line.trim()) continue
          try {
            const msg = JSON.parse(line)
            if (msg.type === 'line') {
              const text: string = msg.text
              setLog(prev => prev + text + '\n')
              // 解析進度：「下載進度 xxx/xxx (xx%)」
              const m = text.match(/下載進度\s+\d+\/\d+\s+\((\d+)%\)/)
              if (m) {
                setSyncProgress(parseInt(m[1]))
                setSyncPhase('下載歷史資料')
              } else if (text.includes('規則分析')) {
                setSyncPhase('規則分析')
                setSyncProgress(null)
              } else if (text.includes('財務報表')) {
                setSyncPhase('同步財務報表')
                setSyncProgress(null)
              }
            } else if (msg.type === 'done') {
              const took = Math.floor((Date.now() - start) / 1000)
              setLastSyncTime(`${new Date().toLocaleTimeString('zh-TW')}（耗時 ${formatElapsed(took)}）`)
              setSyncProgress(100)
              setSyncPhase('完成')
              await fetchRecommendations()
            }
          } catch { /* 非 JSON 行忽略 */ }
        }
      }
    } finally {
      setSyncing(false)
    }
  }

  async function handleAnalyze(mode: 'rule' | 'ml') {
    setAnalyzing(true)
    setLog('')
    const start = Date.now()
    try {
      const res = await fetch('/api/analyze', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode }) })
      const json = await res.json()
      setLog(json.output || json.error || '完成')
      const took = Math.floor((Date.now() - start) / 1000)
      setLastAnalyzeTime(`${new Date().toLocaleTimeString('zh-TW')}（耗時 ${formatElapsed(took)}）`)
      await fetchRecommendations()
    } finally {
      setAnalyzing(false)
    }
  }

  return (
    <div>
      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between mb-6 gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white">今日推薦清單</h1>
          {data?.date && <p className="text-slate-400 text-sm mt-1">資料日期：{data.date}　共 {data.items.length} 檔</p>}
        </div>
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap gap-2">
            <button
              onClick={handleSync}
              disabled={syncing || analyzing}
              className="flex-1 sm:flex-none px-4 py-2 bg-slate-700 hover:bg-slate-600 disabled:opacity-50 text-white text-sm rounded-lg transition-colors min-w-24"
            >
              {syncing ? `同步中… ${formatElapsed(syncElapsed)}` : '同步資料'}
            </button>

            <button
              onClick={() => handleAnalyze('rule')}
              disabled={syncing || analyzing}
              className="flex-1 sm:flex-none px-4 py-2 bg-slate-700 hover:bg-slate-600 disabled:opacity-50 text-white text-sm rounded-lg transition-colors min-w-24"
            >
              {analyzing ? `分析中… ${formatElapsed(analyzeElapsed)}` : '規則分析'}
            </button>
            <button
              onClick={() => handleAnalyze('ml')}
              disabled={syncing || analyzing}
              className="flex-1 sm:flex-none px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm rounded-lg transition-colors min-w-24"
            >
              {analyzing ? `分析中… ${formatElapsed(analyzeElapsed)}` : 'AI 分析'}
            </button>
          </div>
          {syncing && (
            <div className="w-full">
              <div className="flex justify-between text-xs text-slate-400 mb-1">
                <span>{syncPhase}</span>
                {syncProgress !== null && <span>{syncProgress}%</span>}
              </div>
              <div className="w-full h-1.5 bg-slate-700 rounded-full overflow-hidden">
                {syncProgress !== null ? (
                  <div
                    className="h-full bg-blue-500 rounded-full transition-all duration-300"
                    style={{ width: `${syncProgress}%` }}
                  />
                ) : (
                  <div className="h-full bg-blue-500 rounded-full animate-pulse" style={{ width: '100%' }} />
                )}
              </div>
            </div>
          )}
          <div className="text-xs text-slate-500 text-right space-y-0.5">
            {lastSyncTime && <div>最後同步：{lastSyncTime}</div>}
            {lastAnalyzeTime && <div>最後分析：{lastAnalyzeTime}</div>}
          </div>
        </div>
      </div>

      {log && (
        <pre className="mb-4 p-3 bg-slate-800 text-slate-300 text-xs rounded-lg overflow-auto max-h-40 whitespace-pre-wrap">
          {log.split('\n').filter(l => !/[^\x00-\x7F\u4e00-\u9fff\u3400-\u4dbf\u2000-\u206f\uff00-\uffef\s]/.test(l) || l.trim() === '').join('\n')}
        </pre>
      )}

      {loading ? (
        <div className="text-slate-400 text-center py-20">載入中...</div>
      ) : !data?.items.length ? (
        <div className="text-slate-400 text-center py-20">
          <p className="mb-2">尚無推薦資料</p>
          <p className="text-sm">請先點擊「同步資料」取得台股資料，再執行分析</p>
        </div>
      ) : (
        <RecommendationTable items={data.items} />
      )}
    </div>
  )
}

function RecommendationTable({ items }: { items: RecommendationItem[] }) {
  const [signalFilter, setSignalFilter] = useState<'all' | 'buy' | 'watch' | 'neutral'>('all')
  const [tagFilter, setTagFilter] = useState<string | null>(null)

  // AI 概念股排前面（同 signal 內 AI 優先，再依 score 排序）
  const sorted = [...items].sort((a, b) => {
    const aAI = a.tags?.some(t => t.tag === 'AI') ? 1 : 0
    const bAI = b.tags?.some(t => t.tag === 'AI') ? 1 : 0
    if (bAI !== aAI) return bAI - aAI
    return b.score - a.score
  })

  const signalFiltered = signalFilter === 'all' ? sorted : sorted.filter(i => i.signal === signalFilter)

  // 計算 signal filter 後每個 sub_tag 的命中數，依數量排序，0 筆隱藏
  const subTagCounts = new Map<string, number>()
  for (const item of signalFiltered) {
    const subs = new Set(item.tags?.filter(t => t.tag === 'AI' && t.sub_tag).map(t => t.sub_tag!) ?? [])
    for (const sub of subs) subTagCounts.set(sub, (subTagCounts.get(sub) ?? 0) + 1)
  }
  const visibleSubTags = Array.from(subTagCounts.entries())
    .filter(([, cnt]) => cnt > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([sub]) => sub)

  let filtered = tagFilter ? signalFiltered.filter(i => i.tags?.some(t => t.tag === 'AI' && t.sub_tag === tagFilter)) : signalFiltered

  const counts = {
    all: items.length,
    buy: items.filter(i => i.signal === 'buy').length,
    watch: items.filter(i => i.signal === 'watch').length,
    neutral: items.filter(i => i.signal === 'neutral').length,
  }

  const filterBtns: { key: typeof signalFilter; label: string; cls: string }[] = [
    { key: 'all',     label: `全部 ${counts.all}`,       cls: 'bg-slate-600 text-slate-200' },
    { key: 'buy',     label: `買入 ${counts.buy}`,       cls: 'bg-green-900/60 text-green-300 border border-green-700' },
    { key: 'watch',   label: `觀察 ${counts.watch}`,     cls: 'bg-yellow-900/60 text-yellow-300 border border-yellow-700' },
    { key: 'neutral', label: `中立 ${counts.neutral}`,   cls: 'bg-slate-700 text-slate-400' },
  ]

  return (
    <div>
      <div className="flex gap-2 mb-3">
        {filterBtns.map(btn => (
          <button
            key={btn.key}
            onClick={() => setSignalFilter(btn.key)}
            className={`text-xs px-3 py-1.5 rounded-lg transition-colors ${
              signalFilter === btn.key
                ? btn.cls + ' ring-2 ring-white/20'
                : 'bg-slate-800 text-slate-400 hover:bg-slate-700'
            }`}
          >
            {btn.label}
          </button>
        ))}
      </div>
      {visibleSubTags.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-4 pb-3 border-b border-slate-800 items-center">
          <span className="text-[10px] text-slate-500 mr-1">AI 主題</span>
          <button
            onClick={() => setTagFilter(null)}
            className={`text-[11px] px-2.5 py-1 rounded-full transition-colors ${
              tagFilter === null
                ? 'bg-violet-700 text-white'
                : 'bg-slate-800 text-violet-400/70 hover:bg-slate-700'
            }`}
          >
            全部 {signalFiltered.filter(i => i.tags?.some(t => t.tag === 'AI')).length}
          </button>
          {visibleSubTags.map(sub => (
            <button
              key={sub}
              onClick={() => setTagFilter(tagFilter === sub ? null : sub)}
              className={`text-[11px] px-2.5 py-1 rounded-full transition-colors flex items-center gap-1 ${
                tagFilter === sub
                  ? 'bg-violet-700 text-white'
                  : 'bg-slate-800 text-violet-400/70 hover:bg-slate-700'
              }`}
            >
              {sub}
              <span className={`text-[10px] ${tagFilter === sub ? 'text-violet-200' : 'text-slate-500'}`}>
                {subTagCounts.get(sub)}
              </span>
            </button>
          ))}
        </div>
      )}
      {/* 手機版 card 列表 */}
      <div className="sm:hidden flex flex-col gap-2">
        {filtered.map((item) => (
          <a key={item.symbol} href={`/stocks/${item.symbol}`} onClick={() => sessionStorage.setItem(SCROLL_KEY, String(window.scrollY))} className="block bg-slate-800/60 rounded-xl p-3 active:bg-slate-700/60">
            <div className="flex items-start justify-between gap-2 mb-2">
              <div>
                <div className="flex items-center gap-1.5 flex-wrap">
                  <span className="text-blue-400 font-medium">{item.symbol.replace('.TW', '').replace('.TWO', '')}</span>
                  <span className="text-slate-400 text-xs">{item.name}</span>
                  {item.tags?.some(t => t.tag === 'AI') && (
                    <span className="text-[10px] px-1.5 rounded bg-violet-900/60 text-violet-300 border border-violet-700/60">AI</span>
                  )}
                </div>
                {item.tags?.some(t => t.tag === 'AI') && (
                  <div className="flex flex-wrap gap-1 mt-0.5">
                    {[...new Set(item.tags.filter(t => t.tag === 'AI' && t.sub_tag).map(t => t.sub_tag))].map((sub, i) => (
                      <span key={i} className="text-[10px] px-1.5 rounded bg-slate-700 text-violet-400/80">{sub}</span>
                    ))}
                  </div>
                )}
              </div>
              <div className="text-right shrink-0">
                <div className="font-mono text-white">{item.close?.toFixed(2) ?? '-'}</div>
                <div className={`text-xs font-mono ${item.changePct == null ? 'text-slate-500' : item.changePct >= 0 ? 'text-red-400' : 'text-green-400'}`}>
                  {item.changePct != null ? `${item.changePct >= 0 ? '+' : ''}${item.changePct.toFixed(2)}%` : '-'}
                </div>
              </div>
            </div>
            <div className="flex items-center gap-2 mb-2">
              <SignalBadge signal={item.signal} />
              <ScoreBar score={item.score} />
              <span className="text-xs text-slate-500 ml-auto">{item.volume ? item.volume.toLocaleString() + '張' : ''}</span>
            </div>
            <div className="flex flex-wrap gap-1">
              {item.reasons.map((r, i) => {
                const isWarn = r.startsWith('⚠')
                const isDiverg = r.includes('買超／') || r.includes('賣超／')
                return (
                  <span key={i} className={`text-[11px] px-1.5 py-0.5 rounded ${
                    isWarn ? 'bg-red-900/50 text-red-300 border border-red-800/60'
                    : isDiverg ? 'bg-yellow-900/40 text-yellow-300 border border-yellow-700/50'
                    : 'bg-slate-700/80 text-slate-300'
                  }`}>{r}</span>
                )
              })}
            </div>
          </a>
        ))}
      </div>

      {/* 桌面版表格 */}
      <div className="hidden sm:block overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-slate-400 border-b border-slate-700">
            <th className="text-left py-3 px-3">股票</th>
            <th className="text-left py-3 px-3">市場</th>
            <th className="text-right py-3 px-3">收盤價</th>
            <th className="text-right py-3 px-3">漲跌幅</th>
            <th className="text-right py-3 px-3">成交量</th>
            <th className="text-right py-3 px-3">評分</th>
            <th className="text-left py-3 px-3">訊號</th>
            <th className="text-left py-3 px-3">推薦理由</th>
          </tr>
        </thead>
        <tbody>
          {filtered.map((item) => (
            <tr key={item.symbol} className="border-b border-slate-800 hover:bg-slate-800/50 transition-colors">
              <td className="py-3 px-3">
                <div className="flex items-center gap-1.5 whitespace-nowrap">
                  <a href={`/stocks/${item.symbol}`} onClick={() => sessionStorage.setItem(SCROLL_KEY, String(window.scrollY))} className="text-blue-400 hover:text-blue-300 font-medium">
                    {item.symbol.replace('.TW', '').replace('.TWO', '')}
                  </a>
                  <span className="text-slate-400 text-xs">{item.name}</span>
                  {item.tags?.some(t => t.tag === 'AI') && (
                    <span className="text-[10px] px-1.5 py-0 rounded bg-violet-900/60 text-violet-300 border border-violet-700/60 font-medium">AI</span>
                  )}
                </div>
                {item.tags?.some(t => t.tag === 'AI') && (
                  <div className="flex flex-wrap gap-1 mt-0.5">
                    {[...new Set(item.tags.filter(t => t.tag === 'AI' && t.sub_tag).map(t => t.sub_tag))].map((sub, i) => (
                      <span key={i} className="text-[10px] px-1.5 py-0 rounded bg-slate-800 text-violet-400/80">{sub}</span>
                    ))}
                  </div>
                )}
              </td>
              <td className="py-3 px-3 whitespace-nowrap">
                <span className={`text-xs px-2 py-0.5 rounded ${item.market === 'TSE' ? 'bg-blue-900/50 text-blue-300' : 'bg-purple-900/50 text-purple-300'}`}>
                  {item.market === 'TSE' ? '上市' : '上櫃'}
                </span>
              </td>
              <td className="py-3 px-3 text-right font-mono whitespace-nowrap">{item.close?.toFixed(2) ?? '-'}</td>
              <td className={`py-3 px-3 text-right font-mono whitespace-nowrap ${item.changePct == null ? 'text-slate-500' : item.changePct >= 0 ? 'text-red-400' : 'text-green-400'}`}>
                {item.changePct != null ? `${item.changePct >= 0 ? '+' : ''}${item.changePct.toFixed(2)}%` : '-'}
              </td>
              <td className="py-3 px-3 text-right font-mono whitespace-nowrap text-slate-300">
                {item.volume ? item.volume.toLocaleString() + '張' : '-'}
              </td>
              <td className="py-3 px-3 text-right whitespace-nowrap">
                <ScoreBar score={item.score} />
              </td>
              <td className="py-3 px-3 whitespace-nowrap">
                <SignalBadge signal={item.signal} />
              </td>
              <td className="py-3 px-3 min-w-[320px]">
                <div className="flex flex-wrap gap-1.5">
                  {item.reasons.map((r, i) => {
                    const isWarn = r.startsWith('⚠')
                    const isDiverg = r.includes('買超／') || r.includes('賣超／')
                    return (
                      <span key={i} className={`text-xs px-2 py-0.5 rounded whitespace-nowrap ${
                        isWarn
                          ? 'bg-red-900/50 text-red-300 border border-red-800/60'
                          : isDiverg
                          ? 'bg-yellow-900/40 text-yellow-300 border border-yellow-700/50'
                          : 'bg-slate-700/80 text-slate-300'
                      }`}>{r}</span>
                    )
                  })}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </div>
  )
}

function ScoreBar({ score }: { score: number }) {
  const pct = Math.round(score * 100)
  const color = pct >= 70 ? 'bg-green-500' : pct >= 50 ? 'bg-yellow-500' : 'bg-slate-500'
  return (
    <div className="flex items-center gap-2 justify-end">
      <div className="w-16 h-1.5 bg-slate-700 rounded-full overflow-hidden">
        <div className={`h-full ${color} rounded-full`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-slate-300 w-8 text-right">{pct}</span>
    </div>
  )
}

function SignalBadge({ signal }: { signal: string }) {
  const map: Record<string, string> = {
    buy: 'bg-green-900/60 text-green-300 border border-green-700',
    watch: 'bg-yellow-900/60 text-yellow-300 border border-yellow-700',
    neutral: 'bg-slate-700 text-slate-400',
  }
  const label: Record<string, string> = { buy: '買入', watch: '觀察', neutral: '中立' }
  return (
    <span className={`text-xs px-2 py-0.5 rounded ${map[signal] ?? map.neutral}`}>
      {label[signal] ?? signal}
    </span>
  )
}
