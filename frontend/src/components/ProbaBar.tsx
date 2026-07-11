import type { Proba } from '../lib/api'
import { pct } from '../lib/format'

const ROWS: { key: keyof Proba; label: string; barClass: string }[] = [
  { key: 'up', label: '漲', barClass: 'bg-emerald-500' },
  { key: 'range', label: '盤整', barClass: 'bg-amber-400' },
  { key: 'down', label: '跌', barClass: 'bg-rose-500' },
]

export default function ProbaBar({ proba }: { proba: Proba }) {
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
