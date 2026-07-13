import type { ReactNode } from 'react'
import { signedPct } from '../lib/format'

// Small shared primitives so every page reads the same visual language
// (feedback: the old UI was inconsistent and repetitive).

export function Chip({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap ${className}`}>
      {children}
    </span>
  )
}

export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-xl border border-neutral-800 bg-neutral-900/60 ${className}`}>{children}</div>
  )
}

export function SectionTitle({ title, hint }: { title: ReactNode; hint?: ReactNode }) {
  return (
    <div className="mb-3">
      <h2 className="text-sm font-semibold tracking-wide text-neutral-200">{title}</h2>
      {hint && <p className="mt-0.5 text-xs text-neutral-500">{hint}</p>}
    </div>
  )
}

export function Stat({ label, value, sub, tone = 'neutral' }: {
  label: ReactNode
  value: ReactNode
  sub?: ReactNode
  tone?: 'neutral' | 'up' | 'down'
}) {
  const toneCls = tone === 'up' ? 'text-emerald-300' : tone === 'down' ? 'text-rose-300' : 'text-neutral-100'
  return (
    <div>
      <div className="text-xs text-neutral-500">{label}</div>
      <div className={`text-lg font-semibold tabular-nums ${toneCls}`}>{value}</div>
      {sub && <div className="text-xs text-neutral-500">{sub}</div>}
    </div>
  )
}

// A signed percentage return, coloured green/red. The app's most repeated unit.
export function Return({ value, digits = 1, className = '' }: { value: number | null; digits?: number; className?: string }) {
  if (value === null || value === undefined) return <span className="text-neutral-500">--</span>
  const tone = value >= 0 ? 'text-emerald-300' : 'text-rose-300'
  return <span className={`tabular-nums ${tone} ${className}`}>{signedPct(value, digits)}</span>
}

export function Loading({ label = '載入中…' }: { label?: string }) {
  return <p className="text-sm text-neutral-500">{label}</p>
}

export function ErrorMsg({ error }: { error: string }) {
  return <p className="text-sm text-rose-400">載入失敗:{error}</p>
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="text-sm italic text-neutral-500">{children}</p>
}
