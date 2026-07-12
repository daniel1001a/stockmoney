import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type Opportunity, type MarketSummary } from '../lib/api'
import { useApi } from '../lib/useApi'
import {
  DIRECTION_CLASSES, DIRECTION_LABEL, REGIME_CLASSES, pct, signedPct,
} from '../lib/format'
import { Card, Chip, SectionTitle, Loading, ErrorMsg, Empty } from '../components/ui'
import GlossaryTerm from '../components/GlossaryTerm'

function DirectionChip({ d }: { d: string }) {
  return <Chip className={DIRECTION_CLASSES[d] ?? DIRECTION_CLASSES.range}>{DIRECTION_LABEL[d] ?? d}</Chip>
}

function RegimeChip({ label }: { label: string }) {
  return <Chip className={REGIME_CLASSES[label] ?? 'text-neutral-300 bg-neutral-500/10 border-neutral-500/30'}>{label}</Chip>
}

// --- Market briefing strip --------------------------------------------------

function MarketStrip({ m }: { m: MarketSummary }) {
  const dc = m.direction_counts
  const total = (dc.up ?? 0) + (dc.down ?? 0) + (dc.range ?? 0) || 1
  const vixMood =
    m.vix_term_slope !== null && m.vix_term_slope < 0
      ? { t: 'backwardation(恐慌)', c: 'text-rose-300' }
      : { t: 'contango(平靜)', c: 'text-emerald-300' }
  return (
    <Card className="mb-6 p-4">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4 lg:grid-cols-5">
        <div>
          <div className="text-xs text-neutral-500">大盤狀態(多數標的)</div>
          <div className="mt-1"><RegimeChip label={m.dominant_regime ?? '—'} /></div>
        </div>
        <div>
          <div className="text-xs text-neutral-500">方向分布</div>
          <div className="mt-1 flex h-2 w-full overflow-hidden rounded bg-neutral-800">
            <div className="bg-emerald-500" style={{ width: `${((dc.up ?? 0) / total) * 100}%` }} />
            <div className="bg-amber-400" style={{ width: `${((dc.range ?? 0) / total) * 100}%` }} />
            <div className="bg-rose-500" style={{ width: `${((dc.down ?? 0) / total) * 100}%` }} />
          </div>
          <div className="mt-1 text-xs text-neutral-500">
            <span className="text-emerald-400">{dc.up ?? 0} 漲</span> ·{' '}
            <span className="text-amber-300">{dc.range ?? 0} 盤</span> ·{' '}
            <span className="text-rose-400">{dc.down ?? 0} 跌</span>
          </div>
        </div>
        <div>
          <div className="text-xs text-neutral-500">平均信心</div>
          <div className="mt-1 text-lg font-semibold tabular-nums text-neutral-100">{pct(m.avg_conviction)}</div>
        </div>
        <div>
          <div className="text-xs text-neutral-500">VIX / 期限結構</div>
          <div className="mt-1 text-lg font-semibold tabular-nums text-neutral-100">
            {m.vix?.toFixed(1) ?? '--'}
          </div>
          <div className={`text-xs ${vixMood.c}`}>{vixMood.t}</div>
        </div>
        <div className="col-span-2 sm:col-span-4 lg:col-span-1">
          <div className="text-xs text-neutral-500">今日領漲 / 領跌</div>
          <div className="mt-1 space-y-0.5 text-xs">
            {m.top_gainers.slice(0, 2).map((g) => (
              <div key={g.symbol} className="flex justify-between">
                <Link to={`/ticker/${g.symbol}`} className="text-neutral-300 hover:text-neutral-100">{g.symbol}</Link>
                <span className="text-emerald-400 tabular-nums">{signedPct(g.change_pct)}</span>
              </div>
            ))}
            {m.top_losers.slice(0, 1).map((g) => (
              <div key={g.symbol} className="flex justify-between">
                <Link to={`/ticker/${g.symbol}`} className="text-neutral-300 hover:text-neutral-100">{g.symbol}</Link>
                <span className="text-rose-400 tabular-nums">{signedPct(g.change_pct)}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </Card>
  )
}

// --- Top-5 precision picks (今日最有信心) -----------------------------------

function PickCard({ item }: { item: Opportunity }) {
  return (
    <Link
      to={`/ticker/${item.symbol}`}
      className="block rounded-xl border border-neutral-800 bg-neutral-900/60 p-4 transition-colors hover:border-neutral-700 hover:bg-neutral-900"
    >
      <div className="flex items-start justify-between">
        <div>
          <div className="text-lg font-bold text-neutral-50">{item.symbol}</div>
          <div className="text-xs text-neutral-500">{item.sector}</div>
        </div>
        <DirectionChip d={item.predicted_direction} />
      </div>
      <div className="mt-3 flex items-baseline gap-2">
        <span className="text-3xl font-bold tabular-nums text-neutral-50">{pct(item.conviction)}</span>
        <span className="text-xs text-neutral-500">信心</span>
        <div className="ml-auto"><RegimeChip label={item.regime_label} /></div>
      </div>
      <p className="mt-3 line-clamp-2 text-sm text-neutral-300">
        {item.catalyst_headline ?? item.thesis}
      </p>
      {item.backtest && (
        <p className="mt-2 text-xs text-neutral-500">
          回測方向準確率 {pct(item.backtest.overall_accuracy)}
        </p>
      )}
    </Link>
  )
}

// --- Sortable core watchlist table ------------------------------------------

type SortKey = 'conviction' | 'symbol' | 'accuracy'

function WatchlistTable({ items }: { items: Opportunity[] }) {
  const [sort, setSort] = useState<SortKey>('conviction')
  const [dir, setDir] = useState<'asc' | 'desc'>('desc')

  const sorted = useMemo(() => {
    const acc = (o: Opportunity) => o.backtest?.overall_accuracy ?? -1
    const cmp: Record<SortKey, (a: Opportunity, b: Opportunity) => number> = {
      conviction: (a, b) => a.conviction - b.conviction,
      symbol: (a, b) => a.symbol.localeCompare(b.symbol),
      accuracy: (a, b) => acc(a) - acc(b),
    }
    const s = [...items].sort(cmp[sort])
    return dir === 'desc' ? s.reverse() : s
  }, [items, sort, dir])

  function toggle(key: SortKey) {
    if (sort === key) setDir((d) => (d === 'desc' ? 'asc' : 'desc'))
    else {
      setSort(key)
      setDir('desc')
    }
  }

  const arrow = (key: SortKey) => (sort === key ? (dir === 'desc' ? ' ↓' : ' ↑') : '')

  return (
    <div className="overflow-x-auto rounded-xl border border-neutral-800">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-neutral-800 bg-neutral-900/50 text-left text-neutral-500">
            <th className="cursor-pointer px-3 py-2.5 font-normal hover:text-neutral-300" onClick={() => toggle('symbol')}>
              標的{arrow('symbol')}
            </th>
            <th className="px-3 py-2.5 font-normal">方向</th>
            <th className="cursor-pointer px-3 py-2.5 text-right font-normal hover:text-neutral-300" onClick={() => toggle('conviction')}>
              信心{arrow('conviction')}
            </th>
            <th className="px-3 py-2.5 font-normal">市場狀態</th>
            <th className="px-3 py-2.5 font-normal">一句話論點</th>
            <th className="cursor-pointer px-3 py-2.5 text-right font-normal hover:text-neutral-300" onClick={() => toggle('accuracy')}>
              <GlossaryTerm term="overall_accuracy">回測準確率</GlossaryTerm>{arrow('accuracy')}
            </th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((item) => (
            <tr key={item.symbol} className="border-b border-neutral-900 last:border-0 hover:bg-neutral-900/50">
              <td className="px-3 py-2.5">
                <Link to={`/ticker/${item.symbol}`} className="font-semibold text-neutral-50 hover:text-neutral-300">
                  {item.symbol}
                </Link>
                <div className="text-xs text-neutral-600">{item.sector}</div>
              </td>
              <td className="px-3 py-2.5"><DirectionChip d={item.predicted_direction} /></td>
              <td className="px-3 py-2.5 text-right">
                <div className="flex items-center justify-end gap-2">
                  <div className="h-1.5 w-14 overflow-hidden rounded bg-neutral-800">
                    <div className="h-full bg-neutral-400" style={{ width: `${item.conviction * 100}%` }} />
                  </div>
                  <span className="tabular-nums text-neutral-200">{pct(item.conviction)}</span>
                </div>
              </td>
              <td className="px-3 py-2.5"><RegimeChip label={item.regime_label} /></td>
              <td className="max-w-xs px-3 py-2.5">
                <p className="line-clamp-1 text-neutral-300">{item.catalyst_headline ?? item.thesis}</p>
              </td>
              <td className="px-3 py-2.5 text-right tabular-nums text-neutral-400">
                {item.backtest ? pct(item.backtest.overall_accuracy) : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function Opportunities() {
  const { data, loading, error } = useApi(api.opportunities)
  const { data: market } = useApi(api.marketSummary)

  const picks = useMemo(() => (data ? [...data].sort((a, b) => b.conviction - a.conviction).slice(0, 5) : []), [data])

  return (
    <div>
      <div className="mb-5">
        <h1 className="text-2xl font-bold text-neutral-50">今日機會</h1>
        <p className="mt-1 text-sm text-neutral-500">
          早晨一眼掌握全局:大盤風向、最有信心的精選,以及完整核心觀察清單。排序為模型信心啟發式,非已驗證的交易品質排名。
        </p>
      </div>

      {market && <MarketStrip m={market} />}

      {loading && <Loading />}
      {error && <ErrorMsg error={error} />}
      {data && data.length === 0 && <Empty>目前沒有任何標的的預測快取。先跑 scripts/seed_demo_data.py 或 build_dashboard_snapshot.py。</Empty>}

      {data && data.length > 0 && (
        <>
          <section className="mb-8">
            <SectionTitle title="本日精選 · 最有信心的 5 檔" hint="依模型信心挑出的今日重點,點卡片看完整分析。" />
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
              {picks.map((item) => (
                <PickCard key={item.symbol} item={item} />
              ))}
            </div>
          </section>

          <section>
            <SectionTitle title="核心觀察清單" hint="固定深度追蹤的核心標的,點欄位標題可排序。" />
            <WatchlistTable items={data} />
          </section>
        </>
      )}
    </div>
  )
}
