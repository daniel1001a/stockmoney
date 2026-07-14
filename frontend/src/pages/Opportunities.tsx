import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type CockpitCard, type Briefing, type Quote, type SectorRotationEntry } from '../lib/api'
import { useApi } from '../lib/useApi'
import { refreshIntervalMs } from '../lib/refreshCadence'
import { regimeClass, sentimentLabel, num, signedPct, sectorLabel } from '../lib/format'
import { Card, Chip, SectionTitle, Loading, ErrorMsg, Empty, Return } from '../components/ui'
import GlossaryTerm from '../components/GlossaryTerm'

function RegimeChip({ label }: { label: string }) {
  return <Chip className={regimeClass(label)}>{label}</Chip>
}

const GATE_LABEL: Record<string, string> = { green: '🟢 賣方閘門開', red: '🔴 賣方閘門關', unknown: '⚪ 資料不足' }
const GATE_CLASS: Record<string, string> = {
  green: 'text-emerald-300 bg-emerald-500/10 border-emerald-500/40',
  red: 'text-rose-300 bg-rose-500/10 border-rose-500/40',
  unknown: 'text-neutral-400 bg-neutral-500/10 border-neutral-500/40',
}

// Live (yfinance free tier, ~15min-delayed) price overlay -- clearly labelled
// as "即時" and visually distinct from the model's own EOD close, so the two
// are never confused as the same number. Renders nothing when there's no
// quote yet rather than a misleading "--".
function LivePrice({ quote }: { quote: Quote | undefined }) {
  if (!quote || quote.price === null) return null
  const positive = quote.change_pct !== null && quote.change_pct >= 0
  return (
    <div className="mt-0.5 flex items-baseline gap-1.5 text-xs">
      <span className="tabular-nums text-neutral-300">{`$${num(quote.price)}`}</span>
      {quote.change_pct !== null && (
        <span className={`tabular-nums ${positive ? 'text-emerald-400' : 'text-rose-400'}`}>
          {signedPct(quote.change_pct, 1)}
        </span>
      )}
      <span className="text-neutral-600">即時</span>
    </div>
  )
}

// --- Honest guardrail banner --------------------------------------------

function GuardrailBanner({ text }: { text: string }) {
  return (
    <div className="mb-5 rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-2.5 text-sm text-amber-200">
      ⚖️ {text}
    </div>
  )
}

// --- Daily briefing (Lin morning-note style narrative) ----------------------

const NARRATIVE_BASIS_LABEL: Record<Briefing['narrative_basis'], string> = {
  macro_news: '綜合近期總經/事件新聞 + regime + 板塊強弱',
  fallback_regime_sector: '無足夠總經新聞,退回 regime + 板塊強弱簡版摘要',
}

function DailyBriefing({ b }: { b: Briefing }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <Card className="mb-6 p-4">
      <SectionTitle
        title="📋 今日盤前摘要"
        hint={b.as_of_date ? `資料截至 ${b.as_of_date} · ${NARRATIVE_BASIS_LABEL[b.narrative_basis]}` : undefined}
      />
      <p className="text-sm leading-relaxed text-neutral-200">{b.narrative}</p>

      <details className="mt-3 group/details">
        <summary className="cursor-pointer text-xs text-neutral-500 hover:text-neutral-300">
          每檔關鍵價位(展開)
        </summary>
        <div className="mt-2 max-h-64 space-y-1.5 overflow-y-auto pr-1 text-sm text-neutral-300">
          {b.symbol_lines.map((line, i) => (
            <p key={i} className="leading-relaxed">{line}</p>
          ))}
        </div>
      </details>

      {b.sector_rotation.length > 0 && <SectorRotationPanel rotation={b.sector_rotation} />}

      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        className="mt-3 text-xs text-neutral-500 underline decoration-dotted hover:text-neutral-300"
      >
        {expanded ? '收起' : '展開'} 8 年回測誠實結論
      </button>
      {expanded && (
        <div className="mt-2 space-y-1.5 rounded-lg border border-neutral-800 bg-neutral-950/40 p-3 text-xs text-neutral-400">
          <p className="font-medium text-neutral-300">{b.scoreboard.headline}</p>
          <ul className="list-disc space-y-1 pl-4">
            {b.scoreboard.conclusions.map((c) => (
              <li key={c.id}>{c.text}</li>
            ))}
          </ul>
          <p className="mt-2 italic text-neutral-500">{b.scoreboard.so_what}</p>
        </div>
      )}
    </Card>
  )
}

// --- Sector rotation panel (v2 item 5) ---------------------------------------

function SectorRotationPanel({ rotation }: { rotation: SectorRotationEntry[] }) {
  const maxAbs = Math.max(0.001, ...rotation.map((r) => Math.abs(r.ret_1d_avg)))
  return (
    <div className="mt-3 rounded-lg border border-neutral-800/70 bg-neutral-950/40 p-3">
      <div className="mb-2 flex items-center gap-1.5">
        <GlossaryTerm term="sector_rotation" className="text-xs font-medium text-neutral-300">
          板塊輪動(資金今天在哪)
        </GlossaryTerm>
      </div>
      <div className="space-y-1.5">
        {rotation.map((r) => {
          const positive = r.ret_1d_avg >= 0
          const widthPct = Math.min(100, (Math.abs(r.ret_1d_avg) / maxAbs) * 100)
          return (
            <div key={r.sector} className="flex items-center gap-2 text-xs">
              <span className="w-20 shrink-0 text-neutral-400">{r.sector_label}</span>
              <div className="relative h-3 flex-1 rounded bg-neutral-900">
                <div
                  className={`absolute top-0 h-3 rounded ${positive ? 'left-1/2 bg-emerald-500/60' : 'right-1/2 bg-rose-500/60'}`}
                  style={{ width: `${widthPct / 2}%` }}
                />
                <div className="absolute left-1/2 top-0 h-3 w-px bg-neutral-700" />
              </div>
              <Return value={r.ret_1d_avg} digits={1} className="w-14 shrink-0 text-right" />
              <span className="w-20 shrink-0 text-right text-neutral-500">
                5日{r.ret_5d_avg !== null ? <Return value={r.ret_5d_avg} digits={1} /> : '—'}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// --- Per-name decision card ---------------------------------------------

function WhyLine({ card }: { card: CockpitCard }) {
  if (!card.top_news) {
    return <p className="text-sm italic text-neutral-500">近期無新聞</p>
  }
  const s = sentimentLabel(card.top_news.sentiment_score)
  return (
    <p className="line-clamp-2 text-sm text-neutral-300">
      <span className={`mr-1.5 text-xs font-medium ${s.cls}`}>{s.label}</span>
      {card.top_news.headline}
    </p>
  )
}

function LevelRow({ label, value }: { label: string; value: number | null }) {
  return (
    <div className="flex justify-between text-xs">
      <span className="text-neutral-500">{label}</span>
      <span className="tabular-nums text-neutral-300">{value !== null ? num(value) : '—'}</span>
    </div>
  )
}

// v2 items 3 & 4: volume-vs-average and sector-linkage badges. Both are
// plain descriptive state (never a direction call) -- GlossaryTerm gives the
// non-expert user a plain-language "why does this matter" on hover.
const VOLUME_STATE_CLASS: Record<string, string> = {
  放量: 'text-amber-200 bg-amber-500/10 border-amber-500/40',
  縮量: 'text-neutral-400 bg-neutral-500/10 border-neutral-500/40',
  量能正常: 'text-neutral-400 bg-neutral-500/10 border-neutral-500/40',
  資料不足: 'text-neutral-500 bg-neutral-500/10 border-neutral-500/30',
}

const LINKAGE_STATE_CLASS: Record<string, string> = {
  脫離板塊獨走: 'text-violet-200 bg-violet-500/10 border-violet-500/40',
  跟隨板塊同步: 'text-neutral-400 bg-neutral-500/10 border-neutral-500/40',
  不適用: 'text-neutral-500 bg-neutral-500/10 border-neutral-500/30',
  資料不足: 'text-neutral-500 bg-neutral-500/10 border-neutral-500/30',
}

function SignalRow({ card }: { card: CockpitCard }) {
  const v = card.volume_signal
  const l = card.sector_linkage
  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5">
      <GlossaryTerm term="volume_signal">
        <Chip className={VOLUME_STATE_CLASS[v.state]}>
          {v.state}{v.ratio !== null ? ` (${v.ratio.toFixed(1)}x均量)` : ''}
        </Chip>
      </GlossaryTerm>
      <GlossaryTerm term="sector_linkage">
        <Chip className={LINKAGE_STATE_CLASS[l.state]}>{l.state}</Chip>
      </GlossaryTerm>
    </div>
  )
}

function DecisionCard({ card, quote }: { card: CockpitCard; quote: Quote | undefined }) {
  return (
    <Link
      to={`/ticker/${card.symbol}`}
      className="block rounded-xl border border-neutral-800 bg-neutral-900/60 p-4 transition-colors hover:border-neutral-700 hover:bg-neutral-900"
    >
      <div className="flex items-start justify-between">
        <div>
          <div className="text-lg font-bold text-neutral-50">{card.symbol}</div>
          <div className="text-xs text-neutral-500">{sectorLabel(card.sector)}</div>
        </div>
        <div className="text-right">
          <div className="text-lg font-semibold tabular-nums text-neutral-50">
            {card.price !== null ? `$${num(card.price)}` : '—'}
          </div>
          <LivePrice quote={quote} />
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <RegimeChip label={card.regime} />
        <Chip className="border-neutral-700 bg-neutral-800/60 text-neutral-300">{card.breakout_state}</Chip>
      </div>

      <SignalRow card={card} />

      <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 rounded-lg border border-neutral-800/70 bg-neutral-950/40 p-2.5">
        <LevelRow label="20日高" value={card.levels.nday_high} />
        <LevelRow label="20日低" value={card.levels.nday_low} />
        <LevelRow label="前日高" value={card.levels.prev_high} />
        <LevelRow label="前日低" value={card.levels.prev_low} />
        <LevelRow label="SMA20" value={card.levels.sma20} />
        <LevelRow label="SMA50" value={card.levels.sma50} />
      </div>

      <div className="mt-3"><WhyLine card={card} /></div>

      {card.sellput && (
        <div className="mt-3 rounded-lg border border-neutral-800/70 p-2.5">
          <div className="flex items-center justify-between">
            <span className="text-xs text-neutral-500">賣 put 收租構想(示意)</span>
            <Chip className={GATE_CLASS[card.sellput.gate_light]}>{GATE_LABEL[card.sellput.gate_light]}</Chip>
          </div>
          <div className="mt-1 text-sm text-neutral-200">
            履約價 ~{num(card.sellput.strike)}(現價 -{(card.sellput.otm_pct * 100).toFixed(0)}% OTM)
          </div>
          <p className="mt-1 text-xs text-neutral-500">{card.sellput.reason}</p>
        </div>
      )}
    </Link>
  )
}

// --- Page ---------------------------------------------------------------

type SortKey = 'symbol' | 'sector'

export default function Opportunities() {
  // Market-hours-aware silent refresh (see refreshCadence.ts): fastest in the
  // open's first two hours, slower midday, paused overnight/when tab hidden.
  const cadence = { refreshMs: () => refreshIntervalMs() }
  const { data, loading, error } = useApi(api.cockpit, [], cadence)
  const { data: briefing } = useApi(api.briefing, [], cadence)
  const { data: quotes } = useApi(api.quotes, [], cadence)

  const [sort, setSort] = useState<SortKey>('sector')
  const sorted = useMemo(() => {
    if (!data) return []
    const s = [...data]
    if (sort === 'symbol') s.sort((a, b) => a.symbol.localeCompare(b.symbol))
    else s.sort((a, b) => a.sector.localeCompare(b.sector) || a.symbol.localeCompare(b.symbol))
    return s
  }, [data, sort])

  return (
    <div>
      <div className="mb-5">
        <h1 className="text-2xl font-bold text-neutral-50">今日觀察</h1>
        <p className="mt-1 text-sm text-neutral-500">
          誠實決策輔助面板:現價、關鍵價位、市場狀態、新聞,以及回測驗證過的賣方收租閘門。本工具不預測漲跌,判斷永遠由你做。
        </p>
      </div>

      {briefing && <GuardrailBanner text={briefing.guardrail} />}
      {briefing && <DailyBriefing b={briefing} />}

      {loading && <Loading />}
      {error && <ErrorMsg error={error} />}
      {data && data.length === 0 && (
        <Empty>目前沒有任何標的的資料快取。先跑 scripts/seed_demo_data.py 或 nightly_refresh.py。</Empty>
      )}

      {data && data.length > 0 && (
        <section>
          <div className="mb-3 flex items-center justify-between">
            <SectionTitle
              title="核心觀察清單"
              hint="固定深度追蹤的全部核心標的,依板塊分組。點卡片看完整分析與新聞。"
            />
            <div className="flex gap-1 text-xs">
              <button
                type="button"
                onClick={() => setSort('sector')}
                className={`rounded-md border px-2 py-1 ${sort === 'sector' ? 'border-neutral-600 text-neutral-200' : 'border-neutral-800 text-neutral-500'}`}
              >
                依板塊
              </button>
              <button
                type="button"
                onClick={() => setSort('symbol')}
                className={`rounded-md border px-2 py-1 ${sort === 'symbol' ? 'border-neutral-600 text-neutral-200' : 'border-neutral-800 text-neutral-500'}`}
              >
                依代號
              </button>
            </div>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {sorted.map((card) => (
              <DecisionCard key={card.symbol} card={card} quote={quotes?.[card.symbol]} />
            ))}
          </div>
        </section>
      )}
    </div>
  )
}
