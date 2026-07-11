export function pct(x: number | null | undefined, digits = 0): string {
  if (x === null || x === undefined || Number.isNaN(x)) return '--'
  return `${(x * 100).toFixed(digits)}%`
}

export function num(x: number | null | undefined, digits = 2): string {
  if (x === null || x === undefined || Number.isNaN(x)) return '--'
  return x.toFixed(digits)
}

export const DIRECTION_LABEL: Record<string, string> = {
  up: '看漲',
  down: '看跌',
  range: '盤整',
}

export const DIRECTION_CLASSES: Record<string, string> = {
  up: 'text-emerald-400 bg-emerald-950 border-emerald-800',
  down: 'text-rose-400 bg-rose-950 border-rose-800',
  range: 'text-amber-300 bg-amber-950 border-amber-800',
}

export const RISK_LIGHT_CLASSES: Record<string, string> = {
  green: 'bg-emerald-500',
  yellow: 'bg-amber-400',
  red: 'bg-rose-500',
}

// CLAUDE.md §4: regime detection fixes 3 states (0/1/2), not yet given
// canonical labels by the model layer -- this is a display-only convention,
// not a claim about which index means what.
export function regimeLabel(regime: number): string {
  return `regime ${regime}`
}
