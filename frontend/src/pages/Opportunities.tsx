import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type Opportunity, type MarketSummary } from '../lib/api'
import { useApi } from '../lib/useApi'
import { refreshIntervalMs } from '../lib/refreshCadence'
import {
  DIRECTION_CLASSES, DIRECTION_LABEL, regimeClass, pct, signedPct, sentimentLabel,
} from '../lib/format'
import { Card, Chip, SectionTitle, Loading, ErrorMsg, Empty } from '../components/ui'
import GlossaryTerm from '../components/GlossaryTerm'

function DirectionChip({ d }: { d: string }) {
  return <Chip className={DIRECTION_CLASSES[d] ?? DIRECTION_CLASSES.range}>{DIRECTION_LABEL[d] ?? d}</Chip>
}

function RegimeChip({ label }: { label: string }) {
  return <Chip className={regimeClass(label)}>{label}</Chip>
}

// The card's "why" line: prefer our own catalyst thesis, then the freshest real
// headline for the symbol (so every card reflects the news radar, not just the
// few with an LLM catalyst), falling back to the generic model thesis.
function WhyLine({ item }: { item: Opportunity }) {
  if (item.catalyst_headline) {
    return <p className="line-clamp-2 text-sm text-neutral-300">{item.catalyst_headline}</p>
  }
  if (item.top_news) {
    const s = sentimentLabel(item.top_news.sentiment_score)
    return (
      <p className="line-clamp-2 text-sm text-neutral-300">
        <span className={`mr-1.5 text-xs font-medium ${s.cls}`}>{s.label}</span>
        {item.top_news.headline}
      </p>
    )
  }
  return <p className="line-clamp-2 text-sm text-neutral-400">{item.thesis}</p>
}

// --- Market briefing strip --------------------------------------------------

function MarketStrip({ m }: { m: MarketSummary }) {
  const dc = m.direction_counts
  const total = (dc.up ?? 0) + (dc.down ?? 0) + (dc.range ?? 0) || 1
  const vixMood =
    m.vix_term_slope !== null && m.vix_term_slope < 0
      ? { t: 'backwardation(恐慌)', c: 'text-rose-300' }
      : { t: 'contango(平靜)', c: 'text-emerald-300' }
  const sent = m.analyst_sentiment
  return (
    <Card className="mb-6 p-4">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4 lg:grid-cols-6">
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
        <div>
          <div className="text-xs text-neutral-500">機構評級氛圍</div>
          {sent.n_symbols_covered === 0 ? (
            <div className="mt-1 text-sm text-neutral-500">資料不足</div>
          ) : (
            <div className="mt-1 text-xs">
              <span className="text-emerald-400">{sent.bullish} 偏多</span> ·{' '}
              <span className="text-neutral-400">{sent.neutral} 中性</span> ·{' '}
              <span className="text-rose-400">{sent.bearish} 偏空</span>
              <div className="mt-0.5 text-neutral-600">{sent.n_symbols_covered} 檔有足夠評級</div>
            </div>
          )}
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
  // Directional (money-making) confidence headlines the card, NOT max(...),
  // which for a 'range' call would be confidence in going nowhere.
  const conv = item.directional_conviction ?? item.conviction
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
        <span className="text-3xl font-bold tabular-nums text-neutral-50">{pct(conv)}</span>
        <span className="text-xs text-neutral-500">方向信心</span>
        {item.regime_is_trending && (
          <Chip className="ml-auto border-sky-500/30 bg-sky-500/10 text-sky-300">順勢</Chip>
        )}
      </div>
      <div className="mt-1"><RegimeChip label={item.regime_label} /></div>
      <div className="mt-3"><WhyLine item={item} /></div>
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
    // Default "信心" ordering ranks tradeable directional calls first (by
    // directional conviction); a high-confidence 'range' can't jump the queue.
    const dconv = (o: Opportunity) => (o.actionable ? (o.directional_conviction ?? 0) : -1)
    const cmp: Record<SortKey, (a: Opportunity, b: Opportunity) => number> = {
      conviction: (a, b) => dconv(a) - dconv(b),
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
            <tr
              key={item.symbol}
              className={`border-b border-neutral-900 last:border-0 hover:bg-neutral-900/50 ${
                item.actionable ? '' : 'opacity-55'
              }`}
            >
              <td className="px-3 py-2.5">
                <Link to={`/ticker/${item.symbol}`} className="font-semibold text-neutral-50 hover:text-neutral-300">
                  {item.symbol}
                </Link>
                <div className="text-xs text-neutral-600">{item.sector}</div>
              </td>
              <td className="px-3 py-2.5">
                <div className="flex items-center gap-1.5">
                  <DirectionChip d={item.predicted_direction} />
                  {item.actionable && item.regime_is_trending && (
                    <Chip className="border-sky-500/30 bg-sky-500/10 text-sky-300">順勢</Chip>
                  )}
                </div>
              </td>
              <td className="px-3 py-2.5 text-right">
                {item.actionable ? (
                  <div className="flex items-center justify-end gap-2">
                    <div className="h-1.5 w-14 overflow-hidden rounded bg-neutral-800">
                      <div className="h-full bg-neutral-400" style={{ width: `${(item.directional_conviction ?? 0) * 100}%` }} />
                    </div>
                    <span className="tabular-nums text-neutral-200">{pct(item.directional_conviction)}</span>
                  </div>
                ) : (
                  <span className="text-xs text-neutral-600">盤整 · 無方向</span>
                )}
              </td>
              <td className="px-3 py-2.5"><RegimeChip label={item.regime_label} /></td>
              <td className="max-w-xs px-3 py-2.5"><WhyLine item={item} /></td>
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
  // Market-hours-aware silent refresh (see refreshCadence.ts): fastest in the
  // open's first two hours, slower midday, paused overnight/when tab hidden.
  const cadence = { refreshMs: () => refreshIntervalMs() }
  const { data, loading, error } = useApi(api.opportunities, [], cadence)
  const { data: market } = useApi(api.marketSummary, [], cadence)

  // Backend already ranks actionable-first by directional conviction. "精選"
  // = the tradeable directional calls only; a 'range' call makes no options
  // money no matter how confident, so it is never a "pick".
  const picks = useMemo(() => (data ? data.filter((o) => o.actionable).slice(0, 5) : []), [data])
  const rangeCount = useMemo(() => (data ? data.filter((o) => !o.actionable).length : 0), [data])

  return (
    <div>
      <div className="mb-5">
        <h1 className="text-2xl font-bold text-neutral-50">今日機會</h1>
        <p className="mt-1 text-sm text-neutral-500">
          早晨一眼掌握全局:大盤風向、最有機會賺錢的方向性精選,以及完整核心觀察清單。精選 = 有明確漲/跌方向的可交易機會(盤整不列入,短期期權賺不到錢);排序為模型方向信心啟發式,非已驗證的交易品質排名。
        </p>
      </div>

      {market && <MarketStrip m={market} />}

      {loading && <Loading />}
      {error && <ErrorMsg error={error} />}
      {data && data.length === 0 && <Empty>目前沒有任何標的的預測快取。先跑 scripts/seed_demo_data.py 或 build_dashboard_snapshot.py。</Empty>}

      {data && data.length > 0 && (
        <>
          <section className="mb-8">
            <SectionTitle
              title={`本日精選 · ${picks.length} 個方向性機會`}
              hint="有明確漲/跌方向、可用短期期權表達的機會,依模型方向信心排序。點卡片看完整分析。"
            />
            {picks.length > 0 ? (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
                {picks.map((item) => (
                  <PickCard key={item.symbol} item={item} />
                ))}
              </div>
            ) : (
              <Card className="p-4 text-sm text-neutral-400">
                今日核心清單裡沒有明確方向性機會 — {rangeCount} 檔都判為盤整。短期期權在盤整中賺不到錢,因此這是「觀望」訊號,不是清單壞了。完整分析見下方。
              </Card>
            )}
          </section>

          <section>
            <SectionTitle title="核心觀察清單" hint="固定深度追蹤的全部核心標的(含盤整),點欄位標題可排序。盤整標的代表已分析但今日無方向性交易機會。" />
            <WatchlistTable items={data} />
          </section>
        </>
      )}
    </div>
  )
}
