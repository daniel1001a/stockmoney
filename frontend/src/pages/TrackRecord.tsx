import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { useApi } from '../lib/useApi'
import { DIRECTION_LABEL, pct } from '../lib/format'
import Sparkline from '../components/Sparkline'

const OUTCOME_CLASSES: Record<string, string> = {
  win: 'text-emerald-400',
  loss: 'text-rose-400',
}

export default function TrackRecord() {
  const { data, loading, error } = useApi(api.predictions)

  const wins = data?.outcome_counts.win ?? 0
  const losses = data?.outcome_counts.loss ?? 0
  const total = wins + losses

  return (
    <div>
      <h1 className="text-2xl font-semibold text-neutral-50 mb-1">戰績</h1>
      <p className="text-sm text-neutral-500 mb-6">
        每筆預測用 record-watchlist 產生、到期後用 grade 驗證輸贏。勝率只算方向性預測（非盤整），這是誠實數字，不是美化過的展示。
      </p>

      {loading && <p className="text-neutral-500">載入中…</p>}
      {error && <p className="text-rose-400">載入失敗：{error}</p>}

      {data && (
        <div className="space-y-6">
          <section className="rounded-lg border border-neutral-800 bg-neutral-900 p-4">
            <h2 className="text-sm font-medium text-neutral-300 mb-3">勝率趨勢（20筆滾動）</h2>
            {data.rolling_win_rate.length === 0 ? (
              <p className="text-neutral-600 italic text-sm">尚無已驗證的方向性預測</p>
            ) : (
              <Sparkline
                points={data.rolling_win_rate.map((p) => ({ value: p.rolling_win_rate }))}
                colorMode="fixed"
                emptyMessage="尚無已驗證的方向性預測"
              />
            )}
          </section>

          <section className="rounded-lg border border-neutral-800 bg-neutral-900 p-4">
            <h2 className="text-sm font-medium text-neutral-300 mb-3">輸贏分布</h2>
            {total === 0 ? (
              <p className="text-neutral-600 italic text-sm">尚無已驗證紀錄</p>
            ) : (
              <div className="flex items-center gap-6">
                <div>
                  <div className="text-2xl font-semibold text-emerald-400">{wins}</div>
                  <div className="text-xs text-neutral-500">win</div>
                </div>
                <div>
                  <div className="text-2xl font-semibold text-rose-400">{losses}</div>
                  <div className="text-xs text-neutral-500">loss</div>
                </div>
                <div className="flex-1 h-2 rounded bg-neutral-800 overflow-hidden flex">
                  <div className="h-full bg-emerald-500" style={{ width: `${(wins / total) * 100}%` }} />
                  <div className="h-full bg-rose-500" style={{ width: `${(losses / total) * 100}%` }} />
                </div>
                <div className="text-sm text-neutral-400">{pct(wins / total)}</div>
              </div>
            )}
          </section>

          <section className="rounded-lg border border-neutral-800 bg-neutral-900 p-4">
            <h2 className="text-sm font-medium text-neutral-300 mb-3">近期預測</h2>
            {data.recent.length === 0 ? (
              <p className="text-neutral-600 italic text-sm">尚無任何預測紀錄</p>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-neutral-500 text-left border-b border-neutral-800">
                    <th className="py-1 font-normal">日期</th>
                    <th className="py-1 font-normal">標的</th>
                    <th className="py-1 font-normal">預測</th>
                    <th className="py-1 font-normal">狀態</th>
                    <th className="py-1 font-normal">結果</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent.map((p) => {
                    const direction = String(p.predicted_direction)
                    const outcome = p.outcome as string | null
                    return (
                      <tr key={String(p.prediction_id)} className="border-b border-neutral-900">
                        <td className="py-1.5 text-neutral-400">{String(p.trade_date)}</td>
                        <td className="py-1.5">
                          <Link to={`/ticker/${p.symbol}`} className="text-neutral-200 hover:text-neutral-50">
                            {String(p.symbol)}
                          </Link>
                        </td>
                        <td className="py-1.5">{DIRECTION_LABEL[direction] ?? direction}</td>
                        <td className="py-1.5 text-neutral-400">{String(p.status)}</td>
                        <td className={`py-1.5 ${outcome ? OUTCOME_CLASSES[outcome] : 'text-neutral-600'}`}>
                          {outcome ?? '--'}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            )}
          </section>
        </div>
      )}
    </div>
  )
}
