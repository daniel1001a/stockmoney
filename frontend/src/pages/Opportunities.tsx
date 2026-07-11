import { Link } from 'react-router-dom'
import { api, type Opportunity } from '../lib/api'
import { useApi } from '../lib/useApi'
import { DIRECTION_CLASSES, DIRECTION_LABEL, num, pct } from '../lib/format'
import ProbaBar from '../components/ProbaBar'
import GlossaryTerm from '../components/GlossaryTerm'

function thesisFor(item: Opportunity): string {
  if (item.catalyst_headline) return item.catalyst_headline
  return `regime ${item.regime} 下模型判斷${DIRECTION_LABEL[item.predicted_direction] ?? item.predicted_direction}`
}

function driverFor(item: Opportunity): string {
  if (!item.backtest) return '回測快取尚未就緒'
  return `準確率 ${pct(item.backtest.overall_accuracy)}（n=${item.backtest.overall_n}） · Brier ${num(item.backtest.overall_brier, 3)}`
}

function invalidationFor(item: Opportunity): string {
  return `regime 判斷改變，或至 ${item.label_end_date} 未達目標價區間`
}

function OpportunityRow({ item }: { item: Opportunity }) {
  const directionClass = DIRECTION_CLASSES[item.predicted_direction] ?? DIRECTION_CLASSES.range
  const directionLabel = DIRECTION_LABEL[item.predicted_direction] ?? item.predicted_direction

  return (
    <tr className="border-b border-neutral-900 hover:bg-neutral-900/60">
      <td className="py-2.5 pl-3 pr-3">
        <Link to={`/ticker/${item.symbol}`} className="font-semibold text-neutral-50 hover:text-neutral-300">
          {item.symbol}
        </Link>
        <div className="text-xs text-neutral-500">{item.sector}</div>
      </td>
      <td className="py-2.5 pr-3">
        <div className="flex items-center gap-2">
          <span className={`text-xs font-medium px-2 py-0.5 rounded border whitespace-nowrap ${directionClass}`}>
            {directionLabel}
          </span>
          <GlossaryTerm term="conviction" className="text-xs text-neutral-300 tabular-nums">
            {pct(item.conviction)}
          </GlossaryTerm>
        </div>
        <div className="mt-1">
          <ProbaBar proba={item.proba} compact />
        </div>
      </td>
      <td className="py-2.5 pr-3 max-w-xs">
        <p className="text-sm text-neutral-200 line-clamp-2">{thesisFor(item)}</p>
      </td>
      <td className="py-2.5 pr-3 text-xs text-neutral-400 max-w-48">{driverFor(item)}</td>
      <td className="py-2.5 pr-3 text-xs text-neutral-500 max-w-56">{invalidationFor(item)}</td>
    </tr>
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
        <div className="overflow-x-auto rounded-lg border border-neutral-800">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-neutral-500 text-left border-b border-neutral-800 bg-neutral-900/40">
                <th className="py-2 px-3 font-normal">標的</th>
                <th className="py-2 px-3 font-normal">方向 / 信心</th>
                <th className="py-2 px-3 font-normal">一句話論點</th>
                <th className="py-2 px-3 font-normal">
                  <GlossaryTerm term="overall_accuracy">驅動因子</GlossaryTerm>
                </th>
                <th className="py-2 px-3 font-normal">失效條件</th>
              </tr>
            </thead>
            <tbody>
              {data.map((item) => (
                <OpportunityRow key={item.symbol} item={item} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
