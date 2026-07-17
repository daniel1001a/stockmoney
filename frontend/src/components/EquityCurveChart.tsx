interface Series {
  label: string
  color: string
  points: { date: string; value: number }[]
}

interface Props {
  series: Series[]
  width?: number
  height?: number
  emptyMessage?: string
}

// Minimal inline SVG multi-line chart for the Arena's 資金曲線 (equity
// curve) -- no charting library dependency, same reasoning as
// components/Sparkline.tsx. Unlike Sparkline this needs (a) several traders
// overlaid on one shared time axis and (b) a visible zero baseline since
// cumulative P&L crosses zero, so it's a separate small component rather
// than a generalization of Sparkline.
export default function EquityCurveChart({ series, width = 700, height = 220, emptyMessage = '資料不足,尚無足夠已結算紀錄' }: Props) {
  const nonEmpty = series.filter((s) => s.points.length > 0)
  if (nonEmpty.length === 0) {
    return <p className="text-sm italic text-neutral-600">{emptyMessage}</p>
  }

  // Shared x-axis: the union of every trade_date across traders, sorted --
  // traders don't necessarily call on the same days.
  const allDates = Array.from(new Set(nonEmpty.flatMap((s) => s.points.map((p) => p.date)))).sort()
  const allValues = nonEmpty.flatMap((s) => s.points.map((p) => p.value)).concat([0])
  const min = Math.min(...allValues)
  const max = Math.max(...allValues)
  const range = max - min || 1
  const padX = 8
  const padY = 10

  const xFor = (date: string) => {
    const i = allDates.indexOf(date)
    const denom = Math.max(allDates.length - 1, 1)
    return (i / denom) * (width - padX * 2) + padX
  }
  const yFor = (value: number) => height - padY - ((value - min) / range) * (height - padY * 2)
  const zeroY = yFor(0)

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height} preserveAspectRatio="none">
        {/* zero baseline -- cumulative P&L can go negative, so "flat" needs a visible reference */}
        <line x1={padX} x2={width - padX} y1={zeroY} y2={zeroY} stroke="#404040" strokeWidth={1} strokeDasharray="3,3" />
        {nonEmpty.map((s) => {
          const coords = s.points.map((p) => `${xFor(p.date).toFixed(1)},${yFor(p.value).toFixed(1)}`)
          return (
            <g key={s.label}>
              {coords.length >= 2 && (
                <polyline points={coords.join(' ')} fill="none" stroke={s.color} strokeWidth={2} />
              )}
              {s.points.map((p, i) => (
                <circle key={i} cx={xFor(p.date)} cy={yFor(p.value)} r={2.5} fill={s.color} />
              ))}
            </g>
          )
        })}
      </svg>
      <div className="mt-1 flex justify-between text-[10px] text-neutral-600">
        <span>{allDates[0]}</span>
        <span>{allDates[allDates.length - 1]}</span>
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
        {nonEmpty.map((s) => (
          <span key={s.label} className="flex items-center gap-1.5 text-xs text-neutral-400">
            <span className="inline-block h-2 w-2 rounded-full" style={{ backgroundColor: s.color }} />
            {s.label}
          </span>
        ))}
      </div>
    </div>
  )
}
