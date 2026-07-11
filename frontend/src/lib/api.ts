// Typed client for the read-only FastAPI backend (src/stockmoney/api).
// Every shape here mirrors src/stockmoney/api/queries.py's dict output --
// keep the two in sync by hand (no shared schema generation yet, v1).

export interface Proba {
  down: number
  range: number
  up: number
}

export interface Backtest {
  as_of_date: string
  overall_n: number
  overall_accuracy: number
  overall_brier: number
  overall_sharpe: number
  ev_passed_n: number
  ev_passed_win_rate: number | null
  ev_blocked_n: number
  ev_blocked_win_rate: number | null
  ev_of_continuing_now: number | null
}

export interface Opportunity {
  symbol: string
  sector: string
  trade_date: string
  horizon: number
  label_end_date: string
  regime: number
  proba: Proba
  predicted_direction: 'up' | 'down' | 'range'
  conviction: number
  entry_price: number
  target_price_up: number
  target_price_down: number
  feature_values: Record<string, number>
  model_version: string
  backtest: Backtest | null
  catalyst_headline: string | null
}

export interface CatalystDetail {
  as_of_date: string
  catalyst_summary: string
  transmission_chain: string
  novelty_score: number | null
  sentiment_score: number | null
  priced_in_estimate: number | null
  source_refs: string[]
}

export interface PredictionHistoryEntry {
  prediction_id: string
  trade_date: string
  predicted_direction: string
  status: 'pending' | 'graded'
  outcome: 'win' | 'loss' | null
  label_end_date: string
}

export interface PricePoint {
  trade_date: string
  close: number
}

export interface TickerDetail extends Omit<Opportunity, 'catalyst_headline'> {
  history: PredictionHistoryEntry[]
  price_history: PricePoint[]
  catalyst: CatalystDetail | null
}

export interface CatalystSignal {
  signal_id: string
  symbol: string
  as_of_date: string
  catalyst_summary: string
  transmission_chain: string
  novelty_score: number | null
  sentiment_score: number | null
  priced_in_estimate: number | null
  source_refs: string[]
  model_version: string
  available_at: string
}

export interface CatalystsResponse {
  available: boolean
  items: CatalystSignal[]
}

export interface RiskTrigger {
  kind: string
  light: 'yellow' | 'red'
  detail: string
}

export interface PositionRisk {
  position_id: string
  symbol: string
  option_right: 'call' | 'put'
  side: 'long' | 'short'
  entry_date: string
  entry_underlying_price: number
  entry_premium: number
  current_underlying_price: number
  as_of_date: string
  light: 'green' | 'yellow' | 'red'
  triggers: RiskTrigger[]
  notes: string[]
}

export interface RollingWinRatePoint {
  label_end_date: string
  rolling_win_rate: number
}

export interface PredictionsOverview {
  rolling_win_rate: RollingWinRatePoint[]
  outcome_counts: Record<string, number>
  recent: Record<string, unknown>[]
}

export interface WatchlistMember {
  symbol: string
  sector: string
  tier: string
}

export interface WatchlistCandidate {
  proposed_date: string
  symbol: string | null
  theme: string | null
  rationale: string
  evidence_count: number | null
  source_refs: string[]
}

export interface WatchlistResponse {
  core: WatchlistMember[]
  candidates: WatchlistCandidate[]
}

export interface PipelineHealthEntry {
  table: string
  last_status: string
  last_run_at: string
  rows_written: number
  days_since_last_run: number
  is_stale: boolean
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`)
  if (!res.ok) {
    throw new Error(`${path} -> HTTP ${res.status}`)
  }
  return res.json() as Promise<T>
}

export const api = {
  opportunities: () => getJson<Opportunity[]>('/opportunities'),
  ticker: (symbol: string) => getJson<TickerDetail>(`/ticker/${encodeURIComponent(symbol)}`),
  predictions: () => getJson<PredictionsOverview>('/predictions'),
  positions: () => getJson<PositionRisk[]>('/positions'),
  catalysts: () => getJson<CatalystsResponse>('/catalysts'),
  watchlist: () => getJson<WatchlistResponse>('/watchlist'),
  pipelineHealth: () => getJson<PipelineHealthEntry[]>('/pipeline-health'),
}
