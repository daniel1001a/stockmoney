import type { ReactNode } from 'react'

interface Props {
  label: string
  children: ReactNode
}

// Dependency-free hover/focus tooltip. Keyboard-accessible via tabIndex +
// focus-within (see plan doc: no charting/UI library dependency beyond what
// the task requires).
export default function Tooltip({ label, children }: Props) {
  return (
    <span className="relative inline-block group focus-within:z-20" tabIndex={0}>
      {children}
      <span
        role="tooltip"
        className="pointer-events-none absolute bottom-full left-1/2 z-20 mb-1.5 w-max max-w-64 -translate-x-1/2 rounded border border-neutral-700 bg-neutral-800 px-2 py-1.5 text-xs text-neutral-200 opacity-0 shadow-lg transition-opacity group-hover:opacity-100 group-focus-within:opacity-100"
      >
        {label}
      </span>
    </span>
  )
}
