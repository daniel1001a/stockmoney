import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type MarketEvent, type NewsFreshness } from '../lib/api'
import { useApi } from '../lib/useApi'
import { NEWS_TYPE_META, newsTypeMeta, relTime } from '../lib/format'
import { Card, Chip, SectionTitle, Loading, ErrorMsg, Empty } from '../components/ui'
import NewsRow from '../components/NewsRow'

const TYPE_ORDER = ['catalyst', 'analyst_rating', 'earnings', 'macro', 'headline'] as const

// "要是我動不動就回來看,我得要好好翻閱" -- the user's complaint. localStorage
// remembers the last time they left this page so we can mark what's new.
const LAST_SEEN_KEY = 'newsradar_last_seen'

function FreshnessStrip({ freshness, newCount, onMarkRead }: {
  freshness: NewsFreshness | null
  newCount: number
  onMarkRead: () => void
}) {
  if (!freshness) return null
  const { last_updated, last_run, news_last_24h } = freshness
  return (
    <div className="mb-4 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-neutral-800 bg-neutral-900/40 px-3 py-2 text-xs text-neutral-400">
      <span>
        消息最後更新 {last_updated ? relTime(last_updated) : '尚無資料'}
        {last_run && (
          <> · 上次抓取 {last_run.rows_written ?? '—'} 則({last_run.source ?? '—'})</>
        )}
        {' '}· 近24h {news_last_24h} 則
      </span>
      {newCount > 0 && (
        <div className="flex items-center gap-2">
          <Chip className="border-sky-500/40 bg-sky-500/10 text-sky-200">
            自你上次到訪 · 新增 {newCount} 則
          </Chip>
          <button
            onClick={onMarkRead}
            className="rounded-md border border-neutral-700 px-2 py-1 text-neutral-300 hover:bg-neutral-800"
          >
            標記為已讀
          </button>
        </div>
      )}
    </div>
  )
}

function EventStrip({ events }: { events: MarketEvent[] }) {
  if (events.length === 0) return null
  return (
    <Card className="mb-6 p-4">
      <SectionTitle title="未來排程大事" hint="人人都知道、時間已定的事件 — 催化劑推理的另一面。" />
      <div className="flex gap-3 overflow-x-auto pb-1">
        {events.map((e) => (
          <div key={e.event_id} className="min-w-[9rem] rounded-lg border border-neutral-800 bg-neutral-900/60 px-3 py-2">
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-neutral-100">{e.symbol ?? '總經'}</span>
              <Chip className="border-neutral-700 bg-neutral-800 text-neutral-300">{e.event_type_label}</Chip>
            </div>
            <div className="mt-1 text-xs text-neutral-500">
              {e.days_until <= 0 ? '今天' : `${e.days_until} 天後`}
            </div>
          </div>
        ))}
      </div>
    </Card>
  )
}

export default function NewsRadar() {
  const { data, loading, error } = useApi(() => api.news(80), [])
  const { data: events } = useApi(api.events)
  const { data: freshness } = useApi(api.newsFreshness)

  const [type, setType] = useState<string>('all')
  const [symbol, setSymbol] = useState<string>('all')
  const [q, setQ] = useState('')

  // Capture the stored "last seen" timestamp once, before this render writes
  // a new one -- this is the baseline for "what's new". On a genuinely first
  // visit there's nothing stored, so we don't claim every item is "new" (that
  // would just be noise); we only start showing deltas from the second visit
  // onward.
  const [baseline] = useState<string | null>(() => localStorage.getItem(LAST_SEEN_KEY))
  const [cleared, setCleared] = useState(false)

  useEffect(() => {
    // Leave a fresh timestamp for next time regardless of whether the user
    // explicitly marked things read -- otherwise every visit after the first
    // would keep comparing against the same stale baseline forever.
    return () => {
      localStorage.setItem(LAST_SEEN_KEY, new Date().toISOString())
    }
  }, [])

  const newIds = useMemo(() => {
    if (!baseline || cleared || !data) return new Set<string>()
    const cutoff = new Date(baseline).getTime()
    if (Number.isNaN(cutoff)) return new Set<string>()
    const ids = new Set<string>()
    data.forEach((n) => {
      const t = new Date(n.published_at).getTime()
      if (!Number.isNaN(t) && t > cutoff) ids.add(n.item_id)
    })
    return ids
  }, [data, baseline, cleared])

  const markRead = () => {
    localStorage.setItem(LAST_SEEN_KEY, new Date().toISOString())
    setCleared(true)
  }

  const symbols = useMemo(() => {
    const s = new Set<string>()
    data?.forEach((n) => n.symbol && s.add(n.symbol))
    return Array.from(s).sort()
  }, [data])

  const filtered = useMemo(() => {
    if (!data) return []
    const needle = q.trim().toLowerCase()
    return data.filter((n) => {
      if (type !== 'all' && n.item_type !== type) return false
      if (symbol !== 'all' && n.symbol !== symbol) return false
      if (needle && !(`${n.headline} ${n.summary ?? ''} ${n.symbol ?? ''}`.toLowerCase().includes(needle)))
        return false
      return true
    })
  }, [data, type, symbol, q])

  const counts = useMemo(() => {
    const c: Record<string, number> = {}
    data?.forEach((n) => (c[n.item_type] = (c[n.item_type] ?? 0) + 1))
    return c
  }, [data])

  return (
    <div>
      <div className="mb-5">
        <h1 className="text-2xl font-bold text-neutral-50">消息雷達</h1>
        <p className="mt-1 text-sm text-neutral-500">
          所有可能影響股價的消息匯流:我們推理出、市場可能還沒反映的催化劑,以及財報、分析師評級、總經事件。用類型/標的/關鍵字快速篩選。
        </p>
      </div>

      <FreshnessStrip freshness={freshness ?? null} newCount={newIds.size} onMarkRead={markRead} />

      {events && <EventStrip events={events} />}

      {/* Filters */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <button
          onClick={() => setType('all')}
          className={`rounded-full border px-3 py-1 text-xs ${type === 'all' ? 'border-neutral-500 bg-neutral-800 text-neutral-100' : 'border-neutral-800 text-neutral-400 hover:text-neutral-200'}`}
        >
          全部 {data ? `(${data.length})` : ''}
        </button>
        {TYPE_ORDER.map((t) => (
          <button
            key={t}
            onClick={() => setType(t)}
            className={`rounded-full border px-3 py-1 text-xs ${type === t ? newsTypeMeta(t).cls : 'border-neutral-800 text-neutral-400 hover:text-neutral-200'}`}
          >
            {NEWS_TYPE_META[t].label} {counts[t] ? `(${counts[t]})` : ''}
          </button>
        ))}
        <div className="ml-auto flex items-center gap-2">
          <select
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            className="rounded-md border border-neutral-800 bg-neutral-900 px-2 py-1 text-xs text-neutral-300"
          >
            <option value="all">所有標的</option>
            {symbols.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="搜尋標題/內容…"
            className="w-40 rounded-md border border-neutral-800 bg-neutral-900 px-2 py-1 text-xs text-neutral-200 placeholder:text-neutral-600"
          />
        </div>
      </div>

      {loading && <Loading />}
      {error && <ErrorMsg error={error} />}
      {data && (
        <Card>
          {filtered.length === 0 ? (
            <div className="p-4"><Empty>沒有符合條件的消息。試著放寬篩選。</Empty></div>
          ) : (
            <div>
              {filtered.map((n) => (
                <NewsRow key={n.item_id} item={n} isNew={newIds.has(n.item_id)} />
              ))}
            </div>
          )}
        </Card>
      )}
      <p className="mt-3 text-xs text-neutral-600">
        共 {filtered.length} 則。點任一則看詳情與原文連結。新聞標題與財報/總經事件來自 RSS/GDELT 等即時來源;「催化劑推理」欄位是模型對這些消息的推論,非原始報導。
        <Link to="/" className="ml-2 text-neutral-500 hover:text-neutral-300">← 回今日機會</Link>
      </p>
    </div>
  )
}
