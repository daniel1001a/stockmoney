import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type CockpitCard, type Briefing, type Quote, type SectorRotationEntry } from '../lib/api'
import { useApi } from '../lib/useApi'
import { refreshIntervalMs } from '../lib/refreshCadence'
import { regimeClass, sentimentLabel, num, signedPct, sectorLabel, pct, compactMoney, shortDate } from '../lib/format'
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
      <MarketStatsStrip b={b} />

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
// Shrunk from a stacked-bar panel to a single row of compact pills: each
// sector's 1-day return (and 5-day on hover-width tooltip via title attr)
// as one small chip instead of a full-width bar row. Frees vertical space
// on the homepage for MarketStatsStrip below without losing the "where is
// money moving today" read.

function SectorRotationPanel({ rotation }: { rotation: SectorRotationEntry[] }) {
  return (
    <div className="mt-3 rounded-lg border border-neutral-800/70 bg-neutral-950/40 p-2.5">
      <div className="mb-1.5 flex items-center gap-1.5">
        <GlossaryTerm term="sector_rotation" className="text-xs font-medium text-neutral-300">
          板塊輪動(資金今天在哪)
        </GlossaryTerm>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {rotation.map((r) => (
          <div
            key={r.sector}
            title={`5日均${r.ret_5d_avg !== null ? signedPct(r.ret_5d_avg, 1) : '—'}`}
            className="flex items-center gap-1.5 rounded-md border border-neutral-800 bg-neutral-900/60 px-2 py-1 text-xs"
          >
            <span className="text-neutral-400">{r.sector_label}</span>
            <Return value={r.ret_1d_avg} digits={1} />
          </div>
        ))}
      </div>
    </div>
  )
}

// --- Market stats strip (v2 homepage iteration) -------------------------
// Compact replacement for the vertical space the old, taller sector-rotation
// panel used to dominate: VIX level + term-structure mood + today's
// up/down breadth across the watchlist. All three numbers are already
// computed server-side for the narrative paragraph -- this just also
// surfaces them as small stat tiles for a glance-able read.

function MarketStatsStrip({ b }: { b: Briefing }) {
  const moodLabel = b.vix === null ? '資料不足' : (b.vix_term_slope ?? 0) < 0 ? '偏恐慌後仰' : '平靜正常'
  return (
    <div className="mt-3 grid grid-cols-3 gap-1.5 text-center">
      <div className="rounded-lg border border-neutral-800/70 bg-neutral-950/40 px-2 py-1.5">
        <div className="text-[11px] text-neutral-500">VIX</div>
        <div className="mt-0.5 text-sm font-semibold tabular-nums text-neutral-100">
          {b.vix !== null ? b.vix.toFixed(1) : '—'}
        </div>
      </div>
      <GlossaryTerm term="vix_term_mood" className="rounded-lg border border-neutral-800/70 bg-neutral-950/40 px-2 py-1.5">
        <div className="text-[11px] text-neutral-500">期限結構</div>
        <div className="mt-0.5 text-sm font-semibold text-neutral-100">{moodLabel}</div>
      </GlossaryTerm>
      <GlossaryTerm term="market_breadth" className="rounded-lg border border-neutral-800/70 bg-neutral-950/40 px-2 py-1.5">
        <div className="text-[11px] text-neutral-500">今日漲跌家數</div>
        <div className="mt-0.5 text-sm font-semibold tabular-nums text-neutral-100">
          <span className="text-emerald-300">{b.breadth.up}漲</span>
          {' / '}
          <span className="text-rose-300">{b.breadth.down}跌</span>
        </div>
      </GlossaryTerm>
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

// v2 (Task D part 2): three descriptive per-card facts -- IV, today's dollar
// volume, and best-effort next earnings date. None of these is a direction
// call (see glossary.ts card_iv/card_dollar_volume/card_earnings_date);
// each pairs a plain-language GlossaryTerm label with the number/date so a
// non-expert user isn't left staring at unexplained jargon.
function FactsRow({ card }: { card: CockpitCard }) {
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-neutral-400">
      <GlossaryTerm term="card_iv">
        IV {card.iv !== null ? pct(card.iv, 1) : '資料不足'}
      </GlossaryTerm>
      <GlossaryTerm term="card_dollar_volume">
        成交額 {compactMoney(card.dollar_volume)}
      </GlossaryTerm>
      <GlossaryTerm term="card_earnings_date">
        財報日 {shortDate(card.earnings_date)}
      </GlossaryTerm>
    </div>
  )
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

// Collapsed by default (v2: cards were getting cluttered) -- shows just the
// gate light + suggested strike on one line; click reveals the full
// reason/detail text. Click handling stops propagation + prevents default so
// it doesn't also trigger the card's outer <Link> navigation.
function SellPutBox({ sellput }: { sellput: CockpitCard['sellput'] }) {
  const [open, setOpen] = useState(false)
  if (!sellput) return null
  return (
    <div className="mt-3 rounded-lg border border-neutral-800/70 p-2.5">
      <button
        type="button"
        onClick={(e) => {
          e.preventDefault()
          e.stopPropagation()
          setOpen((o) => !o)
        }}
        className="flex w-full items-center justify-between gap-2 text-left"
      >
        <span className="flex min-w-0 items-center gap-1.5 text-xs">
          <Chip className={GATE_CLASS[sellput.gate_light]}>{GATE_LABEL[sellput.gate_light]}</Chip>
          <span className="truncate text-neutral-300">履約價 ~{num(sellput.strike)}</span>
        </span>
        <span className="shrink-0 text-xs text-neutral-600">{open ? '收起 ▲' : '詳情 ▼'}</span>
      </button>
      {open && (
        <div className="mt-2 border-t border-neutral-800/70 pt-2">
          <div className="text-xs text-neutral-500">賣 put 收租構想(示意)</div>
          <div className="mt-1 text-sm text-neutral-200">
            履約價 ~{num(sellput.strike)}(現價 -{(sellput.otm_pct * 100).toFixed(0)}% OTM)
          </div>
          <p className="mt-1 text-xs text-neutral-500">{sellput.reason}</p>
        </div>
      )}
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
      <FactsRow card={card} />

      <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 rounded-lg border border-neutral-800/70 bg-neutral-950/40 p-2.5">
        <LevelRow label="20日高" value={card.levels.nday_high} />
        <LevelRow label="20日低" value={card.levels.nday_low} />
        <LevelRow label="前日高" value={card.levels.prev_high} />
        <LevelRow label="前日低" value={card.levels.prev_low} />
        <LevelRow label="SMA20" value={card.levels.sma20} />
        <LevelRow label="SMA50" value={card.levels.sma50} />
      </div>

      <div className="mt-3"><WhyLine card={card} /></div>

      <SellPutBox sellput={card.sellput} />
    </Link>
  )
}

// --- Page ---------------------------------------------------------------

type SortKey = 'posture' | 'sector' | 'symbol'

// v2 homepage default: group by "current posture" -- purely descriptive of
// where price sits relative to its own recent range/prior day (the same
// breakout_state string already shown as a chip on each card), NEVER a
// forecast of which way it goes next. breakout_state strings come straight
// from cockpit.py's breakout_state(); unrecognized/missing values fall back
// to the neutral bucket per the task's fallback rule.
type PostureKey = 'strong' | 'neutral' | 'weak'

const POSTURE_OF_STATE: Record<string, PostureKey> = {
  創新高: 'strong',
  接近區間高點: 'strong',
  站上前日高點: 'strong',
  區間內盤整: 'neutral',
  創新低: 'weak',
  接近區間低點: 'weak',
  跌破前日低點: 'weak',
}

const POSTURE_SECTION: Record<PostureKey, { title: string; hint: string }> = {
  strong: { title: '偏強姿態(站上關鍵價/突破)', hint: '現價站上前日高點、20日高點附近,或創新高——描述現在位置,非漲跌預測。' },
  neutral: { title: '中性(區間內)', hint: '現價落在近期區間或關鍵均線附近,尚未站上或跌破關鍵價。' },
  weak: { title: '偏弱姿態(跌破關鍵價)', hint: '現價跌破前日低點、20日低點附近,或創新低——描述現在位置,非漲跌預測。' },
}

function postureOf(state: string): PostureKey {
  return POSTURE_OF_STATE[state] ?? 'neutral'
}

// "Strength" within a posture group = how far price has already travelled
// past the level that defines that posture, combined with today's volume
// ratio (both already on the card) -- e.g. a name 3% above its prior-day
// high on 2x volume reads as more "extended" than one just barely above it
// on thin volume. Purely a display-ordering heuristic, not a new signal.
function strengthScore(card: CockpitCard): number {
  const { price, levels } = card
  if (price === null) return 0
  const posture = postureOf(card.breakout_state)
  const refLevel = posture === 'strong' ? levels.prev_high ?? levels.nday_high
    : posture === 'weak' ? levels.prev_low ?? levels.nday_low
    : null
  const extension = refLevel !== null && refLevel !== 0 ? Math.abs((price - refLevel) / refLevel) : 0
  const volRatio = card.volume_signal.ratio ?? 1
  return extension * volRatio
}

export default function Opportunities() {
  // Market-hours-aware silent refresh (see refreshCadence.ts): fastest in the
  // open's first two hours, slower midday, paused overnight/when tab hidden.
  const cadence = { refreshMs: () => refreshIntervalMs() }
  const { data, loading, error } = useApi(api.cockpit, [], cadence)
  const { data: briefing } = useApi(api.briefing, [], cadence)
  const { data: quotes } = useApi(api.quotes, [], cadence)

  const [sort, setSort] = useState<SortKey>('posture')
  const sorted = useMemo(() => {
    if (!data) return []
    const s = [...data]
    if (sort === 'symbol') s.sort((a, b) => a.symbol.localeCompare(b.symbol))
    else if (sort === 'sector') s.sort((a, b) => a.sector.localeCompare(b.sector) || a.symbol.localeCompare(b.symbol))
    return s
  }, [data, sort])

  const postureGroups = useMemo(() => {
    if (sort !== 'posture' || !data) return null
    const buckets: Record<PostureKey, CockpitCard[]> = { strong: [], neutral: [], weak: [] }
    for (const card of data) buckets[postureOf(card.breakout_state)].push(card)
    for (const key of Object.keys(buckets) as PostureKey[]) {
      buckets[key].sort((a, b) => strengthScore(b) - strengthScore(a) || a.symbol.localeCompare(b.symbol))
    }
    return buckets
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
              hint={
                sort === 'posture'
                  ? '當前姿態(描述現在價格在哪,非漲跌預測)——依站上/區間內/跌破關鍵價分組。點卡片看完整分析與新聞。'
                  : '固定深度追蹤的全部核心標的。點卡片看完整分析與新聞。'
              }
            />
            <div className="flex gap-1 text-xs">
              <button
                type="button"
                onClick={() => setSort('posture')}
                className={`rounded-md border px-2 py-1 ${sort === 'posture' ? 'border-neutral-600 text-neutral-200' : 'border-neutral-800 text-neutral-500'}`}
              >
                依姿態
              </button>
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

          {postureGroups ? (
            <div className="space-y-6">
              {(['strong', 'neutral', 'weak'] as PostureKey[]).map((key) => {
                const cards = postureGroups[key]
                if (cards.length === 0) return null
                const section = POSTURE_SECTION[key]
                return (
                  <div key={key}>
                    <GlossaryTerm term="posture_grouping" className="text-xs font-medium text-neutral-400">
                      {section.title} · {cards.length} 檔
                    </GlossaryTerm>
                    <p className="mt-0.5 mb-2 text-xs text-neutral-600">{section.hint}</p>
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                      {cards.map((card) => (
                        <DecisionCard key={card.symbol} card={card} quote={quotes?.[card.symbol]} />
                      ))}
                    </div>
                  </div>
                )
              })}
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {sorted.map((card) => (
                <DecisionCard key={card.symbol} card={card} quote={quotes?.[card.symbol]} />
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  )
}
