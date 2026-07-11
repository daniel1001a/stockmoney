import { Link } from 'react-router-dom'
import { api, type Opportunity } from '../lib/api'
import { useApi } from '../lib/useApi'
import { DIRECTION_CLASSES, DIRECTION_LABEL, num, pct } from '../lib/format'

function OpportunityCard({ item }: { item: Opportunity }) {
  const directionClass = DIRECTION_CLASSES[item.predicted_direction] ?? DIRECTION_CLASSES.range
  const directionLabel = DIRECTION_LABEL[item.predicted_direction] ?? item.predicted_direction

  return (
    <Link
      to={`/ticker/${item.symbol}`}
      className="block rounded-lg border border-neutral-800 bg-neutral-900 p-4 hover:border-neutral-600 transition-colors"
    >
      <div className="flex items-start justify-between">
        <div>
          <div className="text-xl font-semibold text-neutral-50">{item.symbol}</div>
          <div className="text-xs text-neutral-500">{item.sector} · regime {item.regime}</div>
        </div>
        <span className={`text-xs font-medium px-2 py-1 rounded border ${directionClass}`}>
          {directionLabel} {pct(item.conviction)}
        </span>
      </div>

      <div className="mt-3 flex gap-4 text-xs text-neutral-400">
        <span>進場 ${num(item.entry_price)}</span>
        <span>
          目標 ${num(item.target_price_down, 0)}–${num(item.target_price_up, 0)}
        </span>
      </div>

      <div className="mt-2 text-xs text-neutral-500">
        {item.backtest ? (
          <>
            回測準確率 {pct(item.backtest.overall_accuracy)}（n={item.backtest.overall_n}） · Brier{' '}
            {num(item.backtest.overall_brier, 3)}
          </>
        ) : (
          '回測快取尚未就緒'
        )}
      </div>

      <div className="mt-3 pt-3 border-t border-neutral-800 text-sm">
        {item.catalyst_headline ? (
          <p className="text-neutral-200 line-clamp-2">{item.catalyst_headline}</p>
        ) : (
          <p className="text-neutral-600 italic">尚無消息面催化劑資料</p>
        )}
      </div>
    </Link>
  )
}

export default function Opportunities() {
  const { data, loading, error } = useApi(api.opportunities)

  return (
    <div>
      <h1 className="text-2xl font-semibold text-neutral-50 mb-1">今日機會</h1>
      <p className="text-sm text-neutral-500 mb-6">
        依模型信心（conviction）排序 —— 這是啟發式排序，不是已驗證的交易品質排名，請務必自行判斷。
      </p>

      {loading && <p className="text-neutral-500">載入中…</p>}
      {error && <p className="text-rose-400">載入失敗：{error}</p>}

      {data && data.length === 0 && (
        <p className="text-neutral-500">目前沒有任何標的的預測快取。先跑 scripts/build_dashboard_snapshot.py。</p>
      )}

      {data && data.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {data.map((item) => (
            <OpportunityCard key={item.symbol} item={item} />
          ))}
        </div>
      )}
    </div>
  )
}
