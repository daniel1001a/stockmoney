import type { Proba } from '../lib/api'
import { pct } from '../lib/format'
import Tooltip from './Tooltip'

const ROWS: { key: keyof Proba; label: string; barClass: string }[] = [
  { key: 'up', label: '漲', barClass: 'bg-emerald-500' },
  { key: 'range', label: '盤整', barClass: 'bg-amber-400' },
  { key: 'down', label: '跌', barClass: 'bg-rose-500' },
]

interface Props {
  proba: Proba
  compact?: boolean
}

// compact=true renders a single stacked bar (for dense table rows) with the
// three percentages available on hover, instead of three labeled rows.
export default function ProbaBar({ proba, compact = false }: Props) {
  if (compact) {
    return (
      <Tooltip label={ROWS.map((row) => `${row.label} ${pct(proba[row.key])}`).join('　')}>
        <div className="flex h-2 w-24 overflow-hidden rounded bg-neutral-800">
          {ROWS.map((row) => (
            <div key={row.key} className={row.barClass} style={{ width: `${proba[row.key] * 100}%` }} />
          ))}
        </div>
      </Tooltip>
    )
  }

  return (
    <div className="space-y-2">
      {ROWS.map((row) => (
        <div key={row.key} className="flex items-center gap-3 text-sm">
          <span className="w-10 text-neutral-400">{row.label}</span>
          <div className="flex-1 h-2 rounded bg-neutral-800 overflow-hidden">
            <div className={`h-full ${row.barClass}`} style={{ width: `${proba[row.key] * 100}%` }} />
          </div>
          <span className="w-12 text-right text-neutral-300 tabular-nums">{pct(proba[row.key])}</span>
        </div>
      ))}
    </div>
  )
}
