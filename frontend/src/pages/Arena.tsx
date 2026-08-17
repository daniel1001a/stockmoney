import { Link } from 'react-router-dom'
import { api, type LeaderboardEntry, type DivergenceRow, type TraderTradeFeedEntry, type LeagueEquityEntry, type OverallStats } from '../lib/api'
import { useApi } from '../lib/useApi'
import { DIRECTION_LABEL, formatOptionContract, money, num, pct, relTime, signedMoney } from '../lib/format'
import { Card, Chip, SectionTitle, Return, Loading, ErrorMsg, Empty } from '../components/ui'
import EquityCurveChart from '../components/EquityCurveChart'

// Cycled by trader index so the roster can grow past two without picking
// colors per trader_id by hand.
const TRADER_COLORS = ['#60a5fa', '#f472b6', '#34d399', '#fbbf24', '#a78bfa', '#fb7185']

function StandingsCompact({ rows }: { rows: LeaderboardEntry[] }) {
  if (rows.length === 0) return <Empty>尚無交易員參賽紀錄。</Empty>
  return (
    <div className="overflow-x-auto rounded-xl border border-neutral-800">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-neutral-800 bg-neutral-900/50 text-left text-neutral-500">
            <th className="px-3 py-2 font-normal">#</th>
            <th className="px-3 py-2 font-normal">交易員</th>
            <th className="px-3 py-2 text-right font-normal">方向命中率</th>
            <th className="px-3 py-2 text-right font-normal">賺錢率</th>
            <th className="px-3 py-2 text-right font-normal">期望值</th>
            <th className="px-3 py-2 text-right font-normal">累積選擇權損益</th>
            <th className="px-3 py-2 text-right font-normal">已結算筆數</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.trader_id} className="border-b border-neutral-900 last:border-0">
              <td className="px-3 py-2 tabular-nums text-neutral-500">{r.rank}</td>
              <td className="px-3 py-2">
                <Link to={`/trader/${r.trader_id}`} className="font-medium text-neutral-100 hover:text-neutral-300">
                  {r.name}
                </Link>
                {r.data_sufficient === false && (
                  <span className="ml-1.5 text-[11px] text-neutral-600">資料不足</span>
                )}
              </td>
              <td className="px-3 py-2 text-right tabular-nums text-neutral-300">
                {r.hit_rate !== null ? pct(r.hit_rate) : '—'}
              </td>
              <td className="px-3 py-2 text-right tabular-nums text-neutral-300">
                {r.profitable_rate != null ? pct(r.profitable_rate) : '—'}
              </td>
              <td className="px-3 py-2 text-right tabular-nums text-neutral-300">
                {r.expected_value != null ? num(r.expected_value, 3) : '—'}
              </td>
              <td className="px-3 py-2 text-right tabular-nums font-semibold">
                <span className={r.cum_option_pnl > 0 ? 'text-emerald-400' : r.cum_option_pnl < 0 ? 'text-rose-400' : 'text-neutral-400'}>
                  {signedMoney(r.cum_option_pnl)}
                </span>
              </td>
              <td className="px-3 py-2 text-right tabular-nums text-neutral-500">{r.n_graded}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

const HORIZON_LABEL: Record<string, string> = { '1': '1 日', '5': '5 日', '21': '21 日' }

function HorizonBreakdown({ rows }: { rows: LeaderboardEntry[] }) {
  const withHorizons = rows.filter((r) => r.horizons)
  if (withHorizons.length === 0) return <Empty>尚無多視野評分紀錄。</Empty>
  return (
    <div className="overflow-x-auto rounded-xl border border-neutral-800">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-neutral-800 bg-neutral-900/50 text-left text-neutral-500">
            <th className="px-3 py-2 font-normal">交易員</th>
            {['1', '5', '21'].map((h) => (
              <th key={h} className="px-3 py-2 text-right font-normal">{HORIZON_LABEL[h]}(命中率 / 賺錢率 / 期望值)</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {withHorizons.map((r) => (
            <tr key={r.trader_id} className="border-b border-neutral-900 last:border-0">
              <td className="px-3 py-2 text-neutral-200">{r.name}</td>
              {['1', '5', '21'].map((h) => {
                const s = r.horizons?.[h]
                return (
                  <td key={h} className="px-3 py-2 text-right tabular-nums text-neutral-400">
                    {!s || s.n_graded === 0 ? (
                      <span className="text-neutral-600">資料不足</span>
                    ) : (
                      <>
                        {s.hit_rate !== null ? pct(s.hit_rate) : '—'} / {s.profitable_rate != null ? pct(s.profitable_rate) : '—'} / {s.expected_value != null ? num(s.expected_value, 3) : '—'}
                        {s.data_sufficient === false && <span className="ml-1 text-[11px] text-neutral-600">(樣本少)</span>}
                      </>
                    )}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function EquityCurveSection({ entries }: { entries: LeagueEquityEntry[] }) {
  const series = entries.map((e, i) => ({
    label: e.name,
    color: TRADER_COLORS[i % TRADER_COLORS.length],
    points: e.points.map((p) => ({ date: p.trade_date, value: p.cum_option_pnl })),
  }))
  return <EquityCurveChart series={series} />
}

function OverallBanner({ stats }: { stats: OverallStats }) {
  return (
    <Card className="mb-6 p-4">
      <SectionTitle
        title="總體戰績(全部交易員合併)"
        hint="把 5 位交易員已結算的預測當成同一份樣本一起算——這是這個 app 目前實際給出的整體勝率,不是任何單一流派的數字。"
      />
      <div className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
        <div>
          <div className="text-xs text-neutral-500">方向命中率</div>
          <div className="font-semibold text-neutral-100">{stats.hit_rate !== null ? pct(stats.hit_rate) : '—'}</div>
        </div>
        <div>
          <div className="text-xs text-neutral-500">選擇權勝率</div>
          <div className="font-semibold text-neutral-100">{stats.option_win_rate !== null ? pct(stats.option_win_rate) : '—'}</div>
        </div>
        <div>
          <div className="text-xs text-neutral-500">累積選擇權損益(合併)</div>
          <div className={`font-semibold ${stats.cum_option_pnl > 0 ? 'text-emerald-400' : stats.cum_option_pnl < 0 ? 'text-rose-400' : 'text-neutral-100'}`}>
            {signedMoney(stats.cum_option_pnl)}
          </div>
        </div>
        <div>
          <div className="text-xs text-neutral-500">已結算筆數</div>
          <div className="text-neutral-200">{stats.n_graded}(來自 {stats.n_traders} 位交易員)</div>
        </div>
      </div>
    </Card>
  )
}

function RulesBanner() {
  return (
    <Card className="mb-6 p-4">
      <SectionTitle title="比賽規則" hint="每位交易員操作一個模擬短天期選擇權帳戶,以總報酬率一較高下。系統永遠不下真實訂單。" />
      <div className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
        <div><div className="text-xs text-neutral-500">起始資金</div><div className="font-semibold text-neutral-100">{money(100000)}</div></div>
        <div><div className="text-xs text-neutral-500">可用工具</div><div className="text-neutral-200">短天期 call / put</div></div>
        <div><div className="text-xs text-neutral-500">單一部位上限</div><div className="text-neutral-200">資金的 20%</div></div>
        <div><div className="text-xs text-neutral-500">排名依據</div><div className="text-neutral-200">已實現總報酬率</div></div>
      </div>
    </Card>
  )
}

function Leaderboard({ rows }: { rows: LeaderboardEntry[] }) {
  if (rows.length === 0) return <Empty>尚無交易員參賽紀錄。</Empty>
  return (
    <div className="overflow-x-auto rounded-xl border border-neutral-800">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-neutral-800 bg-neutral-900/50 text-left text-neutral-500">
            <th className="px-3 py-2.5 font-normal">#</th>
            <th className="px-3 py-2.5 font-normal">交易員</th>
            <th className="px-3 py-2.5 text-right font-normal">已實現報酬</th>
            <th className="px-3 py-2.5 text-right font-normal">帳戶淨值</th>
            <th className="px-3 py-2.5 text-right font-normal">交易勝率</th>
            <th className="px-3 py-2.5 text-right font-normal">選擇權勝率</th>
            <th className="px-3 py-2.5 text-right font-normal">方向命中率</th>
            <th className="px-3 py-2.5 text-right font-normal">持倉</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.trader_id} className="border-b border-neutral-900 last:border-0 hover:bg-neutral-900/50">
              <td className="px-3 py-3 tabular-nums text-neutral-500">{r.rank}</td>
              <td className="px-3 py-3">
                <Link to={`/trader/${r.trader_id}`} className="font-semibold text-neutral-100 hover:text-neutral-300">
                  {r.name}
                </Link>
                <div className="max-w-xs truncate text-xs text-neutral-600">{r.philosophy}</div>
              </td>
              <td className="px-3 py-3 text-right font-semibold"><Return value={r.realized_return_pct} /></td>
              <td className="px-3 py-3 text-right tabular-nums text-neutral-300">{money(r.equity)}</td>
              <td className="px-3 py-3 text-right tabular-nums text-neutral-300">
                {r.trade_win_rate !== null ? `${pct(r.trade_win_rate)}` : '—'}
                <span className="ml-1 text-xs text-neutral-600">{r.n_closed}筆</span>
              </td>
              <td className="px-3 py-3 text-right tabular-nums text-neutral-300" title="到期用真實選擇權定價算的勝率(含 theta/IV 影響)">
                {r.option_win_rate !== null ? pct(r.option_win_rate) : '—'}
              </td>
              <td className="px-3 py-3 text-right tabular-nums text-neutral-400">
                {r.hit_rate !== null ? pct(r.hit_rate) : '—'}
              </td>
              <td className="px-3 py-3 text-right tabular-nums text-neutral-400">{r.n_open}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function DivergencePanel({ rows }: { rows: DivergenceRow[] }) {
  if (rows.length === 0) return <Empty>近期暫無交易員分歧。</Empty>
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-neutral-800 text-left text-neutral-500">
            <th className="py-1.5 pr-3 font-normal">日期</th>
            <th className="py-1.5 pr-3 font-normal">標的</th>
            <th className="py-1.5 pr-3 font-normal">交易員</th>
            <th className="py-1.5 pr-3 font-normal">方向</th>
            <th className="py-1.5 pr-3 text-center font-normal">分歧</th>
            <th className="py-1.5 text-center font-normal">結果</th>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 12).map((row, i) => (
            <tr key={i} className="border-b border-neutral-900 last:border-0">
              <td className="py-1.5 pr-3 text-neutral-500">{row.trade_date}</td>
              <td className="py-1.5 pr-3 text-neutral-200">{row.symbol}</td>
              <td className="py-1.5 pr-3 text-xs text-neutral-400">{row.trader_id}</td>
              <td className="py-1.5 pr-3">{DIRECTION_LABEL[row.direction] ?? row.direction}</td>
              <td className="py-1.5 pr-3 text-center">
                {row.disagreed ? <span className="text-amber-400">分歧</span> : <span className="text-neutral-600">—</span>}
              </td>
              <td className="py-1.5 text-center text-xs">
                {row.was_right === null ? <span className="text-neutral-600">待定</span>
                  : row.was_right ? <span className="text-emerald-400">對</span>
                    : <span className="text-rose-400">錯</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// The three states a Live Board row can be in, encoded with colour so it is
// scannable at a glance (user feedback: a lone red/green dollar figure was
// "incomplete" — hard to tell holding vs sold-for-profit vs sold-for-loss):
//   holding — position still open (opened N days ago, still held)
//   win     — closed for a realised profit
//   loss    — closed for a realised loss
//   flat    — closed at break-even / P&L not recorded
type TradeState = 'holding' | 'win' | 'loss' | 'flat'

function tradeState(t: TraderTradeFeedEntry): TradeState {
  if (t.status !== 'closed') return 'holding'
  if (t.realized_pnl === null) return 'flat'
  if (t.realized_pnl > 0) return 'win'
  if (t.realized_pnl < 0) return 'loss'
  return 'flat'
}

// label = the outcome badge; border = left-edge accent colour for whole-row
// scanning; text = colour for the P&L figure.
const STATE_META: Record<TradeState, { label: string; chip: string; border: string; text: string }> = {
  holding: { label: '持倉中', chip: 'border-sky-500/40 bg-sky-500/10 text-sky-300', border: 'border-l-sky-500/70', text: 'text-sky-300' },
  win: { label: '獲利平倉', chip: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300', border: 'border-l-emerald-500/80', text: 'text-emerald-300' },
  loss: { label: '虧損平倉', chip: 'border-rose-500/40 bg-rose-500/10 text-rose-300', border: 'border-l-rose-500/80', text: 'text-rose-300' },
  flat: { label: '平倉持平', chip: 'border-neutral-700 bg-neutral-500/10 text-neutral-400', border: 'border-l-neutral-700', text: 'text-neutral-400' },
}

function LiveBoardRow({ t }: { t: TraderTradeFeedEntry }) {
  const isClosed = t.status === 'closed'
  const state = tradeState(t)
  const meta = STATE_META[state]
  // 買入 = opening a long, 賣出 = opening a short; once it's closed we call it
  // by what actually happened to the position (平倉), not the original side.
  const actionLabel = isClosed ? '平倉' : t.side === 'long' ? '買入開倉' : '賣出開倉'
  const contract = formatOptionContract({ symbol: t.symbol, strike: t.strike, right: t.option_right, expiry: t.expiry_date })
  // Qualify the timestamp so an old open position reads as "opened N days ago,
  // still holding" rather than looking like a stale/mistaken "today's trade".
  const timeLabel = isClosed ? `${relTime(t.exit_at ?? t.entry_at)}平倉` : `${relTime(t.entry_at)}進場`

  return (
    <div className={`border-l-2 ${meta.border} px-4 py-3`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Link to={`/trader/${t.trader_id}`} className="text-sm font-semibold text-neutral-100 hover:text-neutral-300">
            {t.trader_name}
          </Link>
          <Chip className={t.option_right === 'call' ? 'border-emerald-500/40 text-emerald-300' : 'border-rose-500/40 text-rose-300'}>
            {actionLabel}
          </Chip>
          <Link to={`/ticker/${t.symbol}`} className="font-mono text-sm text-neutral-200 hover:text-neutral-50">
            {contract}
          </Link>
          <Chip className={meta.chip}>
            {meta.label}
          </Chip>
        </div>
        <div className="flex items-center gap-3 text-xs text-neutral-500">
          {isClosed && t.realized_pnl !== null && (
            <span className={`text-sm font-semibold tabular-nums ${meta.text}`}>
              {signedMoney(t.realized_pnl)}
            </span>
          )}
          <span>{timeLabel}</span>
        </div>
      </div>

      <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-0.5 text-xs text-neutral-500">
        <span>{t.contracts} 口</span>
        <span>進場 標的 ${num(t.entry_underlying)} · 權利金 ${num(t.entry_premium)}</span>
        {isClosed && (
          <span>
            出場 標的 {t.exit_underlying !== null ? `$${num(t.exit_underlying)}` : '—'} · 權利金 {t.exit_premium !== null ? `$${num(t.exit_premium)}` : '—'}
          </span>
        )}
      </div>

      {t.thesis && <p className="mt-1.5 text-xs text-neutral-400">論點:{t.thesis}</p>}
      {isClosed && t.exit_reason && <p className="mt-1 text-xs text-neutral-600">出場原因:{t.exit_reason}</p>}
    </div>
  )
}

function LiveBoard({ rows }: { rows: TraderTradeFeedEntry[] }) {
  if (rows.length === 0) return <Empty>尚無交易紀錄。</Empty>
  return (
    <div className="divide-y divide-neutral-900">
      {rows.map((t) => <LiveBoardRow key={t.trade_id} t={t} />)}
    </div>
  )
}

export default function Arena() {
  const { data: board, loading, error } = useApi(api.leaderboard)
  const { data: equity, loading: equityLoading, error: equityError } = useApi(api.leagueEquity)
  const { data: divergence } = useApi(() => api.divergence(), [])
  const { data: liveBoard, loading: liveBoardLoading, error: liveBoardError } = useApi(() => api.traderTrades(40), [])
  const { data: overall } = useApi(() => api.leagueOverall(), [])

  return (
    <div>
      <div className="mb-5">
        <h1 className="text-2xl font-bold text-neutral-50">交易員競技場</h1>
        <p className="mt-1 text-sm text-neutral-500">
          六個流派各操一個 $100,000 模擬選擇權帳戶,持續出手、到期用真實選擇權損益對帳。看似比賽,實則是模型策略強弱的即時視覺化——現在哪套邏輯在賺錢一眼看穿。點交易員看他的完整帳戶、持倉與每筆交易紀錄。
        </p>
      </div>

      {overall && <OverallBanner stats={overall} />}
      <RulesBanner />

      <section className="mb-8">
        <SectionTitle title="排行榜" hint="以已實現報酬率排名。「交易勝率」是模擬帳戶的實際輸贏;「選擇權勝率」用真實選擇權定價算(方向對但 theta/IV 崩仍可能賠);「方向命中率」只看漲跌判斷對不對。" />
        {loading && <Loading />}
        {error && <ErrorMsg error={error} />}
        {board && <Leaderboard rows={board} />}
      </section>

      <section className="mb-8">
        <SectionTitle title="資金曲線與戰績" hint="累積選擇權損益(已結算)隨時間變化,一眼看出誰在贏;右側精簡榜單以方向命中率與已結算筆數輔助判讀。" />
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <Card className="p-4 lg:col-span-2">
            {equityLoading && <Loading />}
            {equityError && <ErrorMsg error={equityError} />}
            {equity && <EquityCurveSection entries={equity} />}
          </Card>
          <div className="lg:col-span-1">
            {loading && <Loading />}
            {error && <ErrorMsg error={error} />}
            {board && <StandingsCompact rows={board} />}
          </div>
        </div>
      </section>

      <section className="mb-8">
        <SectionTitle title="評分視野(短/中/長)" hint="同一則判斷在 1 / 5 / 21 交易日三個視野各自獨立評分——一個交易員可能短線常對、長線常錯,反之亦然,兩者都要看。" />
        {loading && <Loading />}
        {error && <ErrorMsg error={error} />}
        {board && <HorizonBreakdown rows={board} />}
      </section>

      <section className="mb-8">
        <SectionTitle title="交易動態 (Live Board)" hint="全部交易員的即時交易紀錄,最新在最上面。這是流派競技的過程紀錄,不是操作建議——六次回測已證實,沒有一個流派長期打贏單純持有(見 CLAUDE.md)。" />
        <Card className="p-0">
          {liveBoardLoading && <div className="p-4"><Loading /></div>}
          {liveBoardError && <div className="p-4"><ErrorMsg error={liveBoardError} /></div>}
          {liveBoard && <LiveBoard rows={liveBoard} />}
        </Card>
      </section>

      <section>
        <SectionTitle title="跨交易員分歧雷達" hint="有人看多、有人看空 = 有價值的訊息(CLAUDE.md §8):市場正在用消息篩選贏家輸家。" />
        <Card className="p-4">
          {divergence && <DivergencePanel rows={divergence} />}
        </Card>
      </section>
    </div>
  )
}
