import { api, type LeagueEntry, type DivergenceRow } from '../lib/api'
import { useApi } from '../lib/useApi'
import { num, pct, DIRECTION_LABEL } from '../lib/format'

function StatCell({ value, digits = 2 }: { value: number | null; digits?: number }) {
  if (value === null) return <span className="text-neutral-600">—</span>
  return <span>{num(value, digits)}</span>
}

function HitRateCell({ rate, n }: { rate: number | null; n: number }) {
  if (rate === null || n === 0) return <span className="text-neutral-600">—</span>
  return (
    <span>
      {pct(rate)}{' '}
      <span className="text-neutral-600 text-xs">n={n}</span>
    </span>
  )
}

function LeagueTable({ entries }: { entries: LeagueEntry[] }) {
  if (entries.length === 0) {
    return <p className="text-neutral-600 italic text-sm">尚無交易員參賽紀錄</p>
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-neutral-500 text-left border-b border-neutral-800">
            <th className="py-2 font-normal pr-4">交易員</th>
            <th className="py-2 font-normal pr-4">理念</th>
            <th className="py-2 font-normal pr-4 text-right">命中率(滾動)</th>
            <th className="py-2 font-normal pr-4 text-right">Brier</th>
            <th className="py-2 font-normal pr-4 text-right">avg PnL</th>
            <th className="py-2 font-normal text-right">高信心精準率</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <tr key={entry.trader_id} className="border-b border-neutral-900">
              <td className="py-2 pr-4">
                <span className="text-neutral-100">{entry.name}</span>
                {!entry.active && (
                  <span className="ml-2 text-xs text-neutral-600">已退</span>
                )}
              </td>
              <td className="py-2 pr-4 text-neutral-400 text-xs max-w-[180px] truncate">
                {entry.philosophy}
              </td>
              <td className="py-2 pr-4 text-right tabular-nums">
                <HitRateCell
                  rate={entry.rolling.hit_rate}
                  n={entry.rolling.n_directional}
                />
              </td>
              <td className="py-2 pr-4 text-right tabular-nums">
                <StatCell value={entry.rolling.brier} digits={3} />
              </td>
              <td className="py-2 pr-4 text-right tabular-nums">
                <StatCell value={entry.rolling.avg_pnl} digits={4} />
              </td>
              <td className="py-2 text-right tabular-nums">
                {entry.rolling.high_conviction_precision !== null ? (
                  <span>
                    {pct(entry.rolling.high_conviction_precision)}{' '}
                    <span className="text-neutral-600 text-xs">
                      n={entry.rolling.high_conviction_n}
                    </span>
                  </span>
                ) : (
                  <span className="text-neutral-600">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function DivergenceTable({ rows }: { rows: DivergenceRow[] }) {
  if (rows.length === 0) {
    return <p className="text-neutral-600 italic text-sm">近期暫無分歧紀錄</p>
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-neutral-500 text-left border-b border-neutral-800">
            <th className="py-2 font-normal pr-3">日期</th>
            <th className="py-2 font-normal pr-3">標的</th>
            <th className="py-2 font-normal pr-3">交易員</th>
            <th className="py-2 font-normal pr-3">方向</th>
            <th className="py-2 font-normal pr-3 text-right">信心</th>
            <th className="py-2 font-normal pr-3 text-center">分歧</th>
            <th className="py-2 font-normal text-center">結果</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-b border-neutral-900">
              <td className="py-1.5 pr-3 text-neutral-400">{row.trade_date}</td>
              <td className="py-1.5 pr-3 text-neutral-200">{row.symbol}</td>
              <td className="py-1.5 pr-3 text-neutral-400 text-xs">{row.trader_id}</td>
              <td className="py-1.5 pr-3">
                {DIRECTION_LABEL[row.direction] ?? row.direction}
              </td>
              <td className="py-1.5 pr-3 text-right tabular-nums">{pct(row.conviction)}</td>
              <td className="py-1.5 pr-3 text-center">
                {row.disagreed ? (
                  <span className="text-amber-400">分歧</span>
                ) : (
                  <span className="text-neutral-600">—</span>
                )}
              </td>
              <td className="py-1.5 text-center">
                {row.was_right === null ? (
                  <span className="text-neutral-600 text-xs">待定</span>
                ) : row.was_right ? (
                  <span className="text-emerald-400 text-xs">對</span>
                ) : (
                  <span className="text-rose-400 text-xs">錯</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function League() {
  const { data: leagueData, loading: leagueLoading, error: leagueError } = useApi(
    () => api.league(),
    []
  )
  const { data: divData, loading: divLoading, error: divError } = useApi(
    () => api.divergence(),
    []
  )

  return (
    <div>
      <h1 className="text-2xl font-semibold text-neutral-50 mb-1">聯賽</h1>
      <p className="text-sm text-neutral-500 mb-6">
        每位交易員（理念 + 引擎）每天各自出判斷，收盤後對帳計分，滾動 20 筆排行。
      </p>

      <section className="rounded-lg border border-neutral-800 bg-neutral-900 p-4 mb-6">
        <h2 className="text-sm font-medium text-neutral-300 mb-3">滾動成績單</h2>
        {leagueLoading && <p className="text-neutral-500 text-sm">載入中…</p>}
        {leagueError && (
          <p className="text-rose-400 text-sm">載入失敗：{leagueError}</p>
        )}
        {!leagueLoading && !leagueError && leagueData !== null && (
          <LeagueTable entries={leagueData} />
        )}
      </section>

      <section className="rounded-lg border border-neutral-800 bg-neutral-900 p-4">
        <h2 className="text-sm font-medium text-neutral-300 mb-3">跨交易員分歧雷達</h2>
        {divLoading && <p className="text-neutral-500 text-sm">載入中…</p>}
        {divError && (
          <p className="text-rose-400 text-sm">載入失敗：{divError}</p>
        )}
        {!divLoading && !divError && divData !== null && (
          <DivergenceTable rows={divData} />
        )}
      </section>
    </div>
  )
}
