import { Link } from 'react-router-dom'
import type { NewsItem } from '../lib/api'
import { newsTypeMeta, sentimentLabel, relTime, importanceLabel } from '../lib/format'
import { Chip } from './ui'

// One row in a news list. Clicking opens the in-app detail page (/news/:id) --
// the Robinhood pattern the user asked for: a list of headlines you tap into,
// staying inside the app, with the outbound source link on the detail page.
export default function NewsRow({ item, showSymbol = true }: { item: NewsItem; showSymbol?: boolean }) {
  const meta = newsTypeMeta(item.item_type)
  const sent = sentimentLabel(item.sentiment_score)
  const important = (item.importance ?? 0) >= 0.75
  return (
    <Link
      to={`/news/${item.item_id}`}
      className="block border-b border-neutral-900 px-3 py-3 transition-colors last:border-0 hover:bg-neutral-900/60"
    >
      <div className="flex items-center gap-2">
        <Chip className={meta.cls}>{meta.label}</Chip>
        {showSymbol && item.symbol && (
          <span className="text-xs font-semibold text-neutral-300">{item.symbol}</span>
        )}
        {!item.symbol && <span className="text-xs text-neutral-500">總經 · 全市場</span>}
        {important && <Chip className="border-amber-500/40 bg-amber-500/10 text-amber-200">重大</Chip>}
        <span className="ml-auto text-xs text-neutral-600">{relTime(item.published_at)}</span>
      </div>
      <p className="mt-1.5 text-sm font-medium text-neutral-100">{item.headline}</p>
      {item.summary && <p className="mt-0.5 line-clamp-2 text-xs text-neutral-500">{item.summary}</p>}
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span className={sent.cls}>{sent.label}</span>
        {item.source_name && <span className="text-neutral-600">{item.source_name}</span>}
        {item.importance !== null && item.item_type !== 'catalyst' && (
          <span className="text-neutral-600">影響 {importanceLabel(item.importance)}</span>
        )}
      </div>
    </Link>
  )
}
