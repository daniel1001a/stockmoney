import { Link, useParams } from 'react-router-dom'
import { api } from '../lib/api'
import { useApi } from '../lib/useApi'
import {
  newsTypeMeta, sentimentLabel, noveltyLabel, pricedInLabel, importanceLabel, relTime,
} from '../lib/format'
import { Card, Chip, SectionTitle, Loading, ErrorMsg, Empty } from '../components/ui'

function Metric({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900/60 px-3 py-2">
      <div className="text-xs text-neutral-500">{label}</div>
      <div className={`text-sm font-medium ${tone ?? 'text-neutral-200'}`}>{value}</div>
    </div>
  )
}

export default function NewsDetail() {
  const { id = '' } = useParams()
  const { data, loading, error } = useApi(() => api.newsItem(id), [id])
  const meta = data ? newsTypeMeta(data.item_type) : null
  const sent = data ? sentimentLabel(data.sentiment_score) : null

  return (
    <div className="mx-auto max-w-3xl">
      <Link to="/news" className="text-sm text-neutral-500 hover:text-neutral-300">← 回消息雷達</Link>

      {loading && <div className="mt-4"><Loading /></div>}
      {error && <div className="mt-4"><ErrorMsg error={error} /></div>}
      {!loading && !error && data === null && <div className="mt-4"><Empty>找不到這則消息。</Empty></div>}

      {data && meta && sent && (
        <div className="mt-4 space-y-5">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <Chip className={meta.cls}>{meta.label}</Chip>
              {data.symbol ? (
                <Link to={`/ticker/${data.symbol}`} className="text-sm font-semibold text-neutral-200 hover:text-neutral-50">
                  {data.symbol} →
                </Link>
              ) : (
                <span className="text-sm text-neutral-500">總經 · 全市場</span>
              )}
              <span className="ml-auto text-xs text-neutral-600">{relTime(data.published_at)}</span>
            </div>
            <h1 className="mt-3 text-2xl font-bold leading-snug text-neutral-50">{data.headline}</h1>
            {data.source_name && <p className="mt-1 text-sm text-neutral-500">來源:{data.source_name}</p>}
          </div>

          {data.summary && (
            <Card className="p-4">
              <p className="text-neutral-200">{data.summary}</p>
            </Card>
          )}

          {data.transmission_chain && (
            <Card className="p-4">
              <SectionTitle title="傳導鏈推理" hint="這則催化劑如何一步步影響到標的。" />
              <p className="text-sm text-neutral-300">{data.transmission_chain}</p>
            </Card>
          )}

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Metric label="情緒" value={sent.label} tone={sent.cls} />
            <Metric label="新穎度" value={noveltyLabel(data.novelty_score)} />
            <Metric label="市場反映" value={pricedInLabel(data.priced_in_estimate)} />
            <Metric label="影響程度" value={importanceLabel(data.importance)} />
          </div>

          <div className="flex items-center gap-3">
            {data.url && (
              <a
                href={data.url}
                target="_blank"
                rel="noopener noreferrer"
                className="rounded-md border border-neutral-700 bg-neutral-800 px-3 py-1.5 text-sm text-neutral-200 hover:bg-neutral-700"
              >
                閱讀原文 ↗
              </a>
            )}
            {data.symbol && (
              <Link to={`/ticker/${data.symbol}`} className="text-sm text-neutral-400 hover:text-neutral-200">
                看 {data.symbol} 完整分析 →
              </Link>
            )}
          </div>
          <p className="text-xs text-neutral-600">
            這一層完全獨立於方向模型,僅供裁量參考,不會被混進模型機率(CLAUDE.md §1)。
          </p>
        </div>
      )}
    </div>
  )
}
