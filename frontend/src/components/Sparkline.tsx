interface Props {
  points: { value: number }[]
  width?: number
  height?: number
  emptyMessage?: string
  colorMode?: 'trend' | 'fixed'
  fixedColor?: string
}

// Minimal inline SVG line chart -- no charting library dependency (see plan
// doc: don't add abstractions beyond what the task requires). Generic over
// any {value} series so it covers both the price chart and the win-rate
// trend line.
export default function Sparkline({
  points,
  width = 600,
  height = 120,
  emptyMessage = '資料不足',
  colorMode = 'trend',
  fixedColor = '#60a5fa',
}: Props) {
  if (points.length < 2) {
    return <p className="text-neutral-600 text-sm italic">{emptyMessage}</p>
  }

  const values = points.map((p) => p.value)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const range = max - min || 1
  const pad = 4

  const coords = points.map((p, i) => {
    const x = (i / (points.length - 1)) * (width - pad * 2) + pad
    const y = height - pad - ((p.value - min) / range) * (height - pad * 2)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  })

  const up = values[values.length - 1] >= values[0]
  const stroke = colorMode === 'fixed' ? fixedColor : up ? '#34d399' : '#fb7185'

  return (
    <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height} preserveAspectRatio="none">
      <polyline points={coords.join(' ')} fill="none" stroke={stroke} strokeWidth={2} />
    </svg>
  )
}
