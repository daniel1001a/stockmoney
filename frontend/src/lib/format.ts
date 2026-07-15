export function pct(x: number | null | undefined, digits = 0): string {
  if (x === null || x === undefined || Number.isNaN(x)) return '--'
  return `${(x * 100).toFixed(digits)}%`
}

export function signedPct(x: number | null | undefined, digits = 1): string {
  if (x === null || x === undefined || Number.isNaN(x)) return '--'
  const v = x * 100
  return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}%`
}

export function num(x: number | null | undefined, digits = 2): string {
  if (x === null || x === undefined || Number.isNaN(x)) return '--'
  return x.toFixed(digits)
}

export function money(x: number | null | undefined, digits = 0): string {
  if (x === null || x === undefined || Number.isNaN(x)) return '--'
  const sign = x < 0 ? '-' : ''
  return `${sign}$${Math.abs(x).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`
}

export function signedMoney(x: number | null | undefined): string {
  if (x === null || x === undefined || Number.isNaN(x)) return '--'
  return `${x >= 0 ? '+' : '-'}$${Math.abs(x).toLocaleString('en-US', { maximumFractionDigits: 0 })}`
}

// Compact large-dollar-amount formatting for things like today's dollar
// volume (close x volume): "$1.3B" / "$542.1M" / "$88.4K". USD with a $
// prefix, matching money()/LivePrice's convention elsewhere in the app
// rather than mixing in a Chinese 億/萬 unit alongside a $ sign.
export function compactMoney(x: number | null | undefined): string {
  if (x === null || x === undefined || Number.isNaN(x)) return '--'
  const sign = x < 0 ? '-' : ''
  const abs = Math.abs(x)
  if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(1)}B`
  if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(1)}M`
  if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(1)}K`
  return `${sign}$${abs.toFixed(0)}`
}

// "2026-07-30" -> "7/30". Falls back to "未知" for null/unparseable -- used
// for the cockpit card's best-effort next-earnings-date field, which is
// genuinely unknown for some symbols (never fabricated, see
// stockmoney.data.earnings_calendar).
export function shortDate(iso: string | null | undefined): string {
  if (!iso) return '未知'
  const d = new Date(`${iso.slice(0, 10)}T00:00:00`)
  if (Number.isNaN(d.getTime())) return '未知'
  return `${d.getMonth() + 1}/${d.getDate()}`
}

// Rough "x小時前 / x天前" from an ISO timestamp. Purely cosmetic.
export function relTime(iso: string | null | undefined): string {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const mins = Math.round((Date.now() - then) / 60000)
  if (mins < 60) return `${Math.max(1, mins)} 分鐘前`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return `${hrs} 小時前`
  const days = Math.round(hrs / 24)
  return `${days} 天前`
}

export const DIRECTION_LABEL: Record<string, string> = {
  up: '看漲',
  down: '看跌',
  range: '盤整',
}

export const DIRECTION_CLASSES: Record<string, string> = {
  up: 'text-emerald-300 bg-emerald-500/10 border-emerald-500/40',
  down: 'text-rose-300 bg-rose-500/10 border-rose-500/40',
  range: 'text-amber-200 bg-amber-500/10 border-amber-500/40',
}

// Human-readable sector labels (the raw slugs like "big_tech" look like a bug).
export const SECTOR_LABEL: Record<string, string> = {
  big_tech: '大型科技',
  semiconductor: '半導體',
  energy: '能源',
  financials: '金融',
  semiconductor_etf: '半導體 ETF',
  big_tech_etf: '大型科技 ETF',
}
export const sectorLabel = (s: string | null | undefined): string =>
  (s && SECTOR_LABEL[s]) || s || '—'

// Regime chip colouring. The API's regime label describes volatility / trend
// STRENGTH (低波動震盪盤 / 中波動 / 高波動趨勢盤; the market's mode, NOT a stock's up/down call), not up/down direction, and is
// derived per-fit from each cluster's centroid -- so we colour by the vol tier
// keyword rather than an exact-string map: high vol = caution (rose), low vol =
// calm (emerald), otherwise neutral amber.
export function regimeClass(label: string | null | undefined): string {
  if (label && label.includes('高波動')) return 'text-rose-300 bg-rose-500/10 border-rose-500/30'
  if (label && label.includes('低波動')) return 'text-emerald-300 bg-emerald-500/10 border-emerald-500/30'
  if (label && label.includes('波動')) return 'text-amber-200 bg-amber-500/10 border-amber-500/30'
  return 'text-neutral-300 bg-neutral-500/10 border-neutral-500/30'
}

export const RISK_LIGHT_CLASSES: Record<string, string> = {
  green: 'bg-emerald-500',
  yellow: 'bg-amber-400',
  red: 'bg-rose-500',
}

export const RISK_LIGHT_LABEL: Record<string, string> = {
  green: '正常',
  yellow: '接近觸發',
  red: '已觸發',
}

// --- News / catalyst human labels (feedback: raw numbers mean nothing) ------

export interface NewsTypeMeta {
  label: string
  cls: string
}

export const NEWS_TYPE_META: Record<string, NewsTypeMeta> = {
  catalyst: { label: '催化劑推理', cls: 'text-violet-200 bg-violet-500/10 border-violet-500/40' },
  analyst_rating: { label: '分析師評級', cls: 'text-sky-200 bg-sky-500/10 border-sky-500/40' },
  earnings: { label: '財報', cls: 'text-teal-200 bg-teal-500/10 border-teal-500/40' },
  macro: { label: '總經', cls: 'text-orange-200 bg-orange-500/10 border-orange-500/40' },
  headline: { label: '新聞', cls: 'text-neutral-300 bg-neutral-500/10 border-neutral-500/40' },
}

export function newsTypeMeta(t: string): NewsTypeMeta {
  return NEWS_TYPE_META[t] ?? { label: t, cls: 'text-neutral-300 bg-neutral-500/10 border-neutral-500/40' }
}

// Sentiment -1..1 -> 偏多/中性/偏空 with strength.
export function sentimentLabel(s: number | null | undefined): { label: string; cls: string } {
  if (s === null || s === undefined) return { label: '中性', cls: 'text-neutral-400' }
  if (s >= 0.5) return { label: '強烈偏多', cls: 'text-emerald-300' }
  if (s > 0.15) return { label: '偏多', cls: 'text-emerald-400' }
  if (s < -0.5) return { label: '強烈偏空', cls: 'text-rose-300' }
  if (s < -0.15) return { label: '偏空', cls: 'text-rose-400' }
  return { label: '中性', cls: 'text-neutral-400' }
}

export function noveltyLabel(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  if (n >= 0.7) return '全新消息'
  if (n >= 0.4) return '有新意'
  return '舊聞重炒'
}

// priced_in: 0 not priced -> 1 fully priced. User complained "已消化55%" is opaque.
export function pricedInLabel(p: number | null | undefined): string {
  if (p === null || p === undefined) return '—'
  if (p >= 0.7) return '市場多已反映'
  if (p >= 0.4) return '部分反映'
  return '市場可能還沒反映'
}

// Professional option contract notation, e.g. "MSFT 373.3 Call 7/7".
// symbol + strike (trailing ".0" stripped) + Call/Put + M/D from expiry_date.
export function formatOptionContract({
  symbol, strike, right, expiry,
}: {
  symbol: string
  strike: number
  right: 'call' | 'put'
  expiry: string | null | undefined
}): string {
  const strikeStr = Number.isInteger(strike) ? String(strike) : String(Math.round(strike * 100) / 100)
  const rightLabel = right === 'put' ? 'Put' : 'Call'
  let expiryStr = ''
  if (expiry) {
    const d = new Date(`${expiry.slice(0, 10)}T00:00:00`)
    if (!Number.isNaN(d.getTime())) {
      expiryStr = ` ${d.getMonth() + 1}/${d.getDate()}`
    }
  }
  return `${symbol} ${strikeStr} ${rightLabel}${expiryStr}`
}

export function importanceLabel(i: number | null | undefined): string {
  if (i === null || i === undefined) return '一般'
  if (i >= 0.75) return '重大'
  if (i >= 0.5) return '中等'
  return '一般'
}
