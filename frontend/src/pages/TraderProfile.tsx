import { Link, useParams } from 'react-router-dom'
import { api, type TraderTrade } from '../lib/api'
import { useApi } from '../lib/useApi'
import { DIRECTION_LABEL, money, signedMoney, num, pct } from '../lib/format'
import { Card, Chip, SectionTitle, Stat, Return, Loading, ErrorMsg, Empty } from '../components/ui'

function fmtDate(iso: string | null): string {
  if (!iso) return '—'
  return iso.slice(0, 10)
}

function TradeSide({ t }: { t: TraderTrade }) {
  const label = `${t.side === 'long' ? '買' : '賣'} ${t.option_right === 'call' ? 'Call' : 'Put'}`
  const cls = t.option_right === 'call' ? 'text-emerald-300' : 'text-rose-300'
  return <span className={cls}>{label}</span>
}

function OpenPositions({ rows }: { rows: TraderTrade[] }) {
  if (rows.length === 0) return <Empty>目前沒有未平倉部位。</Empty>
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-neutral-800 text-left text-neutral-500">
            <th className="py-2 pr-4 font-normal">標的</th>
            <th className="py-2 pr-4 font-normal">部位</th>
            <th className="py-2 pr-4 font-normal">履約價</th>
            <th className="py-2 pr-4 font-normal">到期</th>
            <th className="py-2 pr-4 text-right font-normal">進場權利金</th>
            <th className="py-2 pr-4 text-right font-normal">現值(粗估)</th>
            <th className="py-2 text-right font-normal">帳面損益(粗估)</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((t) => (
            <tr key={t.trade_id} className="border-b border-neutral-900 last:border-0">
              <td className="py-2 pr-4">
                <Link to={`/ticker/${t.symbol}`} className="font-medium text-neutral-100 hover:text-neutral-300">{t.symbol}</Link>
                <div className="text-xs text-neutral-600">{t.contracts} 口 · 進場 {fmtDate(t.entry_at)}</div>
              </td>
              <td className="py-2 pr-4"><TradeSide t={t} /></td>
              <td className="py-2 pr-4 tabular-nums text-neutral-300">${num(t.strike, 1)}</td>
              <td className="py-2 pr-4 text-neutral-400">{fmtDate(t.expiry_date)}</td>
              <td className="py-2 pr-4 text-right tabular-nums text-neutral-300">${num(t.entry_premium)}</td>
              <td className="py-2 pr-4 text-right tabular-nums text-neutral-300">
                {t.current_premium_est != null ? `$${num(t.current_premium_est)}` : '—'}
              </td>
              <td className={`py-2 text-right tabular-nums ${(t.unrealized_pnl ?? 0) >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
                {t.unrealized_pnl != null ? signedMoney(t.unrealized_pnl) : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function ClosedTrades({ rows }: { rows: TraderTrade[] }) {
  if (rows.length === 0) return <Empty>尚無已平倉交易。</Empty>
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-neutral-800 text-left text-neutral-500">
            <th className="py-2 pr-4 font-normal">標的</th>
            <th className="py-2 pr-4 font-normal">部位</th>
            <th className="py-2 pr-4 font-normal">進場 → 出場</th>
            <th className="py-2 pr-4 text-right font-normal">權利金</th>
            <th className="py-2 pr-4 text-right font-normal">損益</th>
            <th className="py-2 font-normal">出場原因</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((t) => (
            <tr key={t.trade_id} className="border-b border-neutral-900 last:border-0">
              <td className="py-2 pr-4">
                <Link to={`/ticker/${t.symbol}`} className="font-medium text-neutral-100 hover:text-neutral-300">{t.symbol}</Link>
                <div className="text-xs text-neutral-600">{t.contracts} 口</div>
              </td>
              <td className="py-2 pr-4"><TradeSide t={t} /></td>
              <td className="py-2 pr-4 text-xs text-neutral-400">
                {fmtDate(t.entry_at)} → {fmtDate(t.exit_at)}
              </td>
              <td className="py-2 pr-4 text-right tabular-nums text-neutral-400">
                ${num(t.entry_premium)} → ${t.exit_premium != null ? num(t.exit_premium) : '—'}
              </td>
              <td className={`py-2 pr-4 text-right font-medium tabular-nums ${(t.realized_pnl ?? 0) >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
                {t.realized_pnl != null ? signedMoney(t.realized_pnl) : '—'}
              </td>
              <td className="py-2 text-xs text-neutral-500">{t.exit_reason ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function TraderProfile() {
  const { id = '' } = useParams()
  const { data, loading, error } = useApi(() => api.traderProfile(id), [id])

  return (
    <div>
      <Link to="/arena" className="text-sm text-neutral-500 hover:text-neutral-300">← 回競技場</Link>

      {loading && <div className="mt-4"><Loading /></div>}
      {error && <div className="mt-4"><ErrorMsg error={error} /></div>}
      {!loading && !error && data === null && <div className="mt-4"><Empty>找不到這位交易員。</Empty></div>}

      {data && (
        <div className="mt-4 space-y-6">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="flex items-center gap-3">
                <h1 className="text-2xl font-bold text-neutral-50">{data.name}</h1>
                {data.method_version && (
                  <Chip className="border-neutral-700 bg-neutral-800 text-neutral-400">{data.method_version}</Chip>
                )}
              </div>
              <p className="mt-1 max-w-2xl text-sm text-neutral-400">{data.philosophy}</p>
            </div>
            <div className="text-right">
              <div className="text-xs text-neutral-500">已實現報酬</div>
              <div className="text-2xl font-bold"><Return value={data.portfolio.realized_return_pct} /></div>
            </div>
          </div>

          {/* Account tiles */}
          <Card className="p-4">
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
              <Stat label="帳戶淨值" value={money(data.portfolio.equity)} />
              <Stat label="現金" value={money(data.portfolio.cash)} />
              <Stat label="已實現損益" value={signedMoney(data.portfolio.realized_pnl)}
                tone={data.portfolio.realized_pnl >= 0 ? 'up' : 'down'} />
              <Stat label="未實現(粗估)" value={signedMoney(data.portfolio.unrealized_pnl)}
                tone={data.portfolio.unrealized_pnl >= 0 ? 'up' : 'down'} />
              <Stat label="交易勝率" value={data.portfolio.trade_win_rate !== null ? pct(data.portfolio.trade_win_rate) : '—'}
                sub={`${data.portfolio.n_closed} 筆已平倉`} />
              <Stat label="最佳 / 最差" value={
                <span className="text-sm">
                  <span className="text-emerald-300">{signedMoney(data.portfolio.best_trade)}</span>
                  {' / '}
                  <span className="text-rose-300">{signedMoney(data.portfolio.worst_trade)}</span>
                </span>
              } />
            </div>
            <p className="mt-3 text-xs text-neutral-600">
              起始資金 {money(data.portfolio.starting_capital)} · {data.rules.instrument} · 單一部位上限 {pct(data.rules.max_position_pct)} · {data.rules.note}
            </p>
          </Card>

          <Card className="p-4">
            <SectionTitle title="目前持倉" hint="尚未平倉的模擬部位;現值為 delta≈0.5 的粗估,非真實報價。" />
            <OpenPositions rows={data.open_positions} />
          </Card>

          <Card className="p-4">
            <SectionTitle title="交易紀錄" hint="每一筆進出場的完整明細 — 贏在哪、輸在哪、為什麼出場。" />
            <ClosedTrades rows={data.closed_trades} />
          </Card>

          <Card className="p-4">
            <SectionTitle title="近期預測" hint="這位交易員最近的每日出手與對帳結果。" />
            {data.recent_predictions.length === 0 ? (
              <Empty>尚無預測紀錄。</Empty>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-neutral-800 text-left text-neutral-500">
                      <th className="py-2 pr-4 font-normal">日期</th>
                      <th className="py-2 pr-4 font-normal">標的</th>
                      <th className="py-2 pr-4 font-normal">方向</th>
                      <th className="py-2 pr-4 text-right font-normal">信心</th>
                      <th className="py-2 pr-4 font-normal">理由</th>
                      <th className="py-2 font-normal">結果</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.recent_predictions.map((p, i) => (
                      <tr key={i} className="border-b border-neutral-900 last:border-0">
                        <td className="py-2 pr-4 text-neutral-500">{p.trade_date}</td>
                        <td className="py-2 pr-4">
                          <Link to={`/ticker/${p.symbol}`} className="text-neutral-200 hover:text-neutral-50">{p.symbol}</Link>
                        </td>
                        <td className="py-2 pr-4">{DIRECTION_LABEL[p.direction] ?? p.direction}</td>
                        <td className="py-2 pr-4 text-right tabular-nums text-neutral-300">{pct(p.conviction)}</td>
                        <td className="max-w-xs py-2 pr-4"><span className="line-clamp-1 text-xs text-neutral-500">{p.rationale}</span></td>
                        <td className="py-2 text-xs">
                          {p.outcome === 'win' ? <span className="text-emerald-400">贏</span>
                            : p.outcome === 'loss' ? <span className="text-rose-400">輸</span>
                              : <span className="text-neutral-600">待驗證</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </div>
      )}
    </div>
  )
}
