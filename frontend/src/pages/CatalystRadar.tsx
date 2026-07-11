import { Link } from 'react-router-dom'
import { api, type CatalystSignal } from '../lib/api'
import { useApi } from '../lib/useApi'
import { num, pct } from '../lib/format'
import GlossaryTerm from '../components/GlossaryTerm'
import ComingSoon from '../components/ComingSoon'

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
        <GlossaryTerm term="novelty_score" className="text-neutral-500">
          新穎度 {num(item.novelty_score, 2)}
        </GlossaryTerm>
        <GlossaryTerm term="sentiment_score" className={sentimentClass}>
          情緒 {num(item.sentiment_score, 2)}
        </GlossaryTerm>
        <GlossaryTerm term="priced_in_estimate" className="text-neutral-500">
          已消化 {pct(item.priced_in_estimate)}
        </GlossaryTerm>
        <span className="text-neutral-500">{item.source_refs.length} 則來源</span>
      </div>
    </div>
  )
}

export default function CatalystRadar() {
  const { data, loading, error } = useApi(api.catalysts)

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold text-neutral-50 mb-1">消息雷達</h1>
        <p className="text-sm text-neutral-500 mb-4">
          分成兩區：我們自己推理出、市場可能還沒消化的判斷，以及已經公開排程、大家都知道的大事。這一層完全獨立於方向模型，僅供裁量參考。
        </p>

        <h2 className="text-sm font-medium text-neutral-300 mb-3">我們推理出的（市場可能尚未消化）</h2>
        <p className="text-xs text-neutral-600 mb-3">依新穎度 × 情緒強度排序，把可能還沒被市場充分反映的催化劑排到前面。</p>

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

      <div>
        <h2 className="text-sm font-medium text-neutral-300 mb-3">已知大事 / 未來排程消息</h2>
        <ComingSoon
          title="尚未串接事件日曆"
          body="財報日曆、Fed會議、CPI/NFP等排程消息（CLAUDE.md §2 已規劃）目前還沒有對應的資料來源與 API，這裡先誠實留空，不是資料遺失。"
        />
      </div>
    </div>
  )
}
