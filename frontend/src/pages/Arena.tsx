import { Link } from 'react-router-dom'
import { api, type LeaderboardEntry, type PositionRisk, type DivergenceRow } from '../lib/api'
import { useApi } from '../lib/useApi'
import {
  DIRECTION_LABEL, RISK_LIGHT_CLASSES, RISK_LIGHT_LABEL, money, pct, num,
} from '../lib/format'
import { Card, SectionTitle, Return, Loading, ErrorMsg, Empty } from '../components/ui'

function RulesBanner() {
  return (
    <Card className="mb-6 p-4">
      <SectionTitle title="比賽規則" hint="每位交易員操作一個模擬短天期選擇權帳戶,以總報酬率一較高下。系統永遠不下真實訂單。" />
      <div className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
        <div><div className="text-xs text-neutral-500">起始資金</div><div className="font-semibold text-neutral-100">{money(25000)}</div></div>
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

function PositionsPanel({ rows }: { rows: PositionRisk[] }) {
  if (rows.length === 0) return <Empty>目前沒有你自己的未平倉選擇權部位。</Empty>
  return (
    <div className="space-y-2">
      {rows.map((p) => (
        <div key={p.position_id} className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-neutral-800 bg-neutral-900/60 px-3 py-2.5">
          <span className={`h-2.5 w-2.5 rounded-full ${RISK_LIGHT_CLASSES[p.light]}`} title={RISK_LIGHT_LABEL[p.light]} />
          <Link to={`/ticker/${p.symbol}`} className="font-semibold text-neutral-100 hover:text-neutral-300">{p.symbol}</Link>
          <span className="text-xs text-neutral-400">
            {p.side === 'long' ? '買' : '賣'}{p.option_right === 'call' ? ' Call' : ' Put'}
          </span>
          <span className="text-xs text-neutral-500">進場 ${num(p.entry_underlying_price)} · 現價 ${num(p.current_underlying_price)}</span>
          <span className="ml-auto text-xs">
            {p.triggers.length > 0 ? (
              <span className="text-amber-300">{p.triggers.map((t) => t.detail).join(' · ')}</span>
            ) : (
              <span className="text-neutral-600">燈號:{RISK_LIGHT_LABEL[p.light]}</span>
            )}
          </span>
        </div>
      ))}
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

export default function Arena() {
  const { data: board, loading, error } = useApi(api.leaderboard)
  const { data: positions } = useApi(api.positions)
  const { data: divergence } = useApi(() => api.divergence(), [])

  return (
    <div>
      <div className="mb-5">
        <h1 className="text-2xl font-bold text-neutral-50">交易員競技場</h1>
        <p className="mt-1 text-sm text-neutral-500">
          每個流派操一個模擬選擇權帳戶競賽,持續出手、對帳、進步。點交易員看他的完整帳戶、持倉與交易紀錄。
        </p>
      </div>

      <RulesBanner />

      <section className="mb-8">
        <SectionTitle title="排行榜" hint="以已實現報酬率排名;命中率、勝率、持倉為輔助欄位。" />
        {loading && <Loading />}
        {error && <ErrorMsg error={error} />}
        {board && <Leaderboard rows={board} />}
      </section>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card className="p-4">
          <SectionTitle title="你的持倉風控" hint="你自己的未平倉選擇權(非模擬),🟢正常 🟡接近 🔴已觸發。" />
          {positions && <PositionsPanel rows={positions} />}
        </Card>
        <Card className="p-4">
          <SectionTitle title="跨交易員分歧雷達" hint="有人看多、有人看空 = 有價值的訊息(CLAUDE.md §8)。" />
          {divergence && <DivergencePanel rows={divergence} />}
        </Card>
      </div>
    </div>
  )
}
