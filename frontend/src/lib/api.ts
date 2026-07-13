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
  regime_label: string
  thesis: string
  proba: Proba
  predicted_direction: 'up' | 'down' | 'range'
  conviction: number
  actionable: boolean
  directional_conviction: number | null
  regime_is_trending: boolean
  entry_price: number
  target_price_up: number
  target_price_down: number
  feature_values: Record<string, number>
  model_version: string
  backtest: Backtest | null
  catalyst_headline: string | null
  top_news: {
    item_id: string
    headline: string
    sentiment_score: number | null
    importance: number | null
    published_at: string
  } | null
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

export interface NewsItem {
  item_id: string
  symbol: string | null
  item_type: 'catalyst' | 'analyst_rating' | 'earnings' | 'macro' | 'headline'
  headline: string
  summary: string | null
  url: string | null
  source_name: string | null
  published_at: string
  sentiment_score: number | null
  importance: number | null
  novelty_score: number | null
  priced_in_estimate: number | null
  transmission_chain: string | null
  source_refs: string[]
}

export interface TickerDetail extends Omit<Opportunity, 'catalyst_headline'> {
  history: PredictionHistoryEntry[]
  price_history: PricePoint[]
  catalyst: CatalystDetail | null
  news: NewsItem[]
  analyst_sentiment: AnalystSentiment
}

export interface MarketMover {
  symbol: string
  close: number
  change_pct: number
}

export interface MarketAnalystSentiment {
  n_symbols_covered: number
  bullish: number
  neutral: number
  bearish: number
}

export interface AnalystSentiment {
  n_ratings: number
  n_scored: number
  avg_sentiment: number | null
  sufficient_data: boolean
  latest_headline: string | null
  latest_url: string | null
  latest_published_at: string | null
}

export interface MarketSummary {
  as_of_date: string | null
  n_symbols: number
  direction_counts: Record<string, number>
  regime_counts: Record<string, number>
  dominant_regime: string | null
  avg_conviction: number | null
  vix: number | null
  vix_term_slope: number | null
  analyst_sentiment: MarketAnalystSentiment
  top_gainers: MarketMover[]
  top_losers: MarketMover[]
}

export interface MarketEvent {
  event_id: string
  symbol: string | null
  event_type: string
  event_type_label: string
  scheduled_at: string
  status: string | null
  days_until: number
}

export interface LeaderboardEntry {
  rank: number
  trader_id: string
  name: string
  philosophy: string
  active: boolean
  starting_capital: number
  realized_pnl: number
  unrealized_pnl: number
  equity: number
  total_return_pct: number | null
  realized_return_pct: number | null
  n_closed: number
  n_open: number
  trade_win_rate: number | null
  best_trade: number | null
  worst_trade: number | null
  hit_rate: number | null
  brier: number | null
  n_directional: number
  option_win_rate: number | null
  avg_option_pnl: number | null
}

export interface TraderTrade {
  trade_id: string
  symbol: string
  option_right: 'call' | 'put'
  side: 'long' | 'short'
  strike: number
  expiry_date: string
  contracts: number
  entry_at: string
  entry_underlying: number
  entry_premium: number
  exit_at: string | null
  exit_underlying: number | null
  exit_premium: number | null
  realized_pnl: number | null
  status: 'open' | 'closed'
  thesis: string | null
  exit_reason: string | null
  current_underlying?: number | null
  current_premium_est?: number | null
  unrealized_pnl?: number | null
}

export interface ContestRules {
  starting_capital: number
  instrument: string
  max_position_pct: number
  scoring: string
  note: string
}

export interface TraderProfile {
  trader_id: string
  name: string
  philosophy: string
  active: boolean
  method_version: string | null
  method_spec: string | null
  portfolio: {
    starting_capital: number
    cash: number
    max_position_pct: number
    instrument_scope: string
    inception_date: string
    realized_pnl: number
    unrealized_pnl: number
    equity: number
    total_return_pct: number | null
    realized_return_pct: number | null
    n_closed: number
    n_open: number
    trade_win_rate: number | null
    best_trade: number | null
    worst_trade: number | null
  }
  rules: ContestRules
  open_positions: TraderTrade[]
  closed_trades: TraderTrade[]
  recent_predictions: {
    trade_date: string
    symbol: string
    direction: string
    conviction: number
    rationale: string
    regime: number | null
    regime_label: string
    status: string
    outcome: string | null
    label_end_date: string
  }[]
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

// --- Trader League Arena ---

export interface Trader {
  trader_id: string
  name: string
  philosophy: string
  engine_key: string
  active: boolean
  added_date: string
  removed_date: string | null
}

// Output of league_table.compute_stats
export interface TraderStats {
  n_graded: number
  n_directional: number
  hit_rate: number | null
  brier: number | null
  avg_pnl: number | null
  cum_pnl: number
  high_conviction_threshold: number
  high_conviction_n: number
  high_conviction_precision: number | null
}

export interface RollingStats extends TraderStats {
  window: number
}

export interface LeagueEntry {
  trader_id: string
  name: string
  philosophy: string
  active: boolean
  overall: TraderStats
  rolling: RollingStats
  by_regime: Record<string, TraderStats>
}

export interface TraderPredictionEntry {
  trader_id: string
  trade_date: string
  direction: 'up' | 'down' | 'range'
  conviction: number
  rationale: string
  invalidation: string
  regime: number | null
  horizon: number
  label_end_date: string
  status: 'pending' | 'graded'
  outcome: 'win' | 'loss' | null
  method_version: string
}

export interface TickerTradersConsensus {
  agree: boolean
  directions: string[]
  n_traders: number
}

export interface TickerTradersResponse {
  symbol: string
  trade_date: string
  traders: TraderPredictionEntry[]
  consensus: TickerTradersConsensus
}

export interface DivergenceRow {
  trade_date: string
  symbol: string
  trader_id: string
  direction: string
  conviction: number
  disagreed: boolean
  was_right: boolean | null
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`)
  if (!res.ok) {
    throw new Error(`${path} -> HTTP ${res.status}`)
  }
  return res.json() as Promise<T>
}

// Returns null on 404 (honest "no data yet") instead of throwing.
async function getJsonOrNull<T>(path: string): Promise<T | null> {
  const res = await fetch(`/api${path}`)
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`)
  return res.json() as Promise<T>
}

export interface Quote {
  price: number | null
  prev_close: number | null
  change_pct: number | null
  as_of: string | null
}

export const api = {
  opportunities: () => getJson<Opportunity[]>('/opportunities'),
  quotes: () => getJson<Record<string, Quote>>('/quotes'),
  ticker: (symbol: string) => getJson<TickerDetail>(`/ticker/${encodeURIComponent(symbol)}`),
  predictions: () => getJson<PredictionsOverview>('/predictions'),
  positions: () => getJson<PositionRisk[]>('/positions'),
  catalysts: () => getJson<CatalystsResponse>('/catalysts'),
  marketSummary: () => getJson<MarketSummary>('/market-summary'),
  news: (limit?: number) => getJson<NewsItem[]>(`/news${limit !== undefined ? `?limit=${limit}` : ''}`),
  newsItem: (id: string) => getJsonOrNull<NewsItem>(`/news/${encodeURIComponent(id)}`),
  events: () => getJson<MarketEvent[]>('/events'),
  leaderboard: () => getJson<LeaderboardEntry[]>('/leaderboard'),
  traderProfile: (id: string) => getJsonOrNull<TraderProfile>(`/traders/${encodeURIComponent(id)}`),
  watchlist: () => getJson<WatchlistResponse>('/watchlist'),
  pipelineHealth: () => getJson<PipelineHealthEntry[]>('/pipeline-health'),
  league: (window?: number) =>
    getJson<LeagueEntry[]>(`/league${window !== undefined ? `?window=${window}` : ''}`),
  traders: () => getJson<Trader[]>('/traders'),
  tickerTraders: (symbol: string) =>
    getJsonOrNull<TickerTradersResponse>(`/ticker/${encodeURIComponent(symbol)}/traders`),
  divergence: (hours?: number) =>
    getJson<DivergenceRow[]>(`/divergence${hours !== undefined ? `?hours=${hours}` : ''}`),
}
