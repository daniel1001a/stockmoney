import { Link } from 'react-router-dom'
import { api, type CatalystSignal } from '../lib/api'
import { useApi } from '../lib/useApi'
import { num, pct } from '../lib/format'

function CatalystCard({ item }: { item: CatalystSignal }) {
  const sentimentClass =
    item.sentiment_score !== null && item.sentiment_score > 0
      ? 'text-emerald-400'
      : item.sentiment_score !== null && item.sentiment_score < 0
        ? 'text-rose-400'
        : 'text-neutral-400'

  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900 p-4">
      <div className="flex items-start justify-between">
        <Link to={`/ticker/${item.symbol}`} className="text-lg font-semibold text-neutral-50 hover:text-neutral-300">
          {item.symbol}
        </Link>
        <span className="text-xs text-neutral-500">{item.as_of_date}</span>
      </div>
      <p className="mt-2 text-sm text-neutral-100">{item.catalyst_summary}</p>
      <p className="mt-1 text-sm text-neutral-400">{item.transmission_chain}</p>
      <div className="mt-3 flex gap-4 text-xs">
        <span className="text-neutral-500">新穎度 {num(item.novelty_score, 2)}</span>
        <span className={sentimentClass}>情緒 {num(item.sentiment_score, 2)}</span>
        <span className="text-neutral-500">已消化 {pct(item.priced_in_estimate)}</span>
        <span className="text-neutral-500">{item.source_refs.length} 則來源</span>
      </div>
    </div>
  )
}

export default function CatalystRadar() {
  const { data, loading, error } = useApi(api.catalysts)

  return (
    <div>
      <h1 className="text-2xl font-semibold text-neutral-50 mb-1">消息雷達</h1>
      <p className="text-sm text-neutral-500 mb-6">
        依新穎度 × 情緒強度排序 —— 把市場可能還沒完全消化的催化劑排到前面。這一層完全獨立於模型判斷，僅供裁量參考。
      </p>

      {loading && <p className="text-neutral-500">載入中…</p>}
      {error && <p className="text-rose-400">載入失敗：{error}</p>}

      {data && data.items.length === 0 && (
        <p className="text-neutral-500">
          目前沒有新鮮的催化劑訊號 —— 需先跑 <code className="text-neutral-400">scripts/classify_scan.py</code> 與{' '}
          <code className="text-neutral-400">scripts/synthesize_catalysts.py</code>。
        </p>
      )}

      {data && data.items.length > 0 && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {data.items.map((item) => (
            <CatalystCard key={item.signal_id} item={item} />
          ))}
        </div>
      )}
    </div>
  )
}
