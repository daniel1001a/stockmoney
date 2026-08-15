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

export interface NewsFreshnessRun {
  source: string | null
  rows_written: number | null
  finished_at: string | null
  status: string | null
}

export interface NewsFreshness {
  last_updated: string | null
  last_run: NewsFreshnessRun | null
  news_last_24h: number
  median_ingest_gap_minutes: number | null
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
  cum_option_pnl: number
  n_graded: number
}

export interface EquityPoint {
  trade_date: string
  symbol: string
  direction: string
  pnl: number
  cum_pnl: number
  option_pnl: number
  cum_option_pnl: number
}

export interface LeagueEquityEntry {
  trader_id: string
  name: string
  philosophy: string
  active: boolean
  points: EquityPoint[]
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

// A trade row joined to the trader's display name/philosophy -- powers the
// Arena "交易動態 (Live Board)" feed across ALL traders (GET /api/trader-trades).
export interface TraderTradeFeedEntry {
  trade_id: string
  trader_id: string
  trader_name: string
  philosophy: string
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
  // Wave D option metrics (present in the JSON; optional here for back-compat)
  option_win_rate?: number | null
  avg_option_pnl?: number | null
  cum_option_pnl?: number
  n_option_graded?: number
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

export interface WinRatePoint {
  trade_date: string
  n: number
  hit_rate: number
}

export interface MethodProposal {
  proposal_id: string
  trader_id: string
  from_version: string | null
  to_version: string | null
  rationale: string
  status: string
  reviewed_by: string | null
  reviewed_date: string | null
  source_review_date: string | null
}

// /api/league/overall: one pooled scorecard across every trader's graded
// calls -- "what's our overall hit rate/results", not any one trader's.
export interface OverallStats {
  window: number | null
  n_traders: number
  n_graded: number
  n_directional: number
  hit_rate: number | null
  brier: number | null
  avg_pnl: number | null
  cum_pnl: number
  n_option_graded: number
  option_win_rate: number | null
  avg_option_pnl: number | null
  cum_option_pnl: number
}

export interface MethodVersion {
  method_version: string
  effective_date: string
  status: string
}

// /api/league/training: per-trader scorecard + win-rate-over-time + the
// self-improvement proposal / method-version history (是否在進步).
export interface LeagueTrainingEntry extends LeagueEntry {
  win_rate_series: WinRatePoint[]
  proposals: MethodProposal[]
  method_versions: MethodVersion[]
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

// --- Honest Lin cockpit (no direction prediction, see cockpit.py) ----------

export interface ScoreboardFinding {
  id: string
  text: string
}

export interface ScoreboardSummary {
  as_of: string
  headline: string
  conclusions: ScoreboardFinding[]
  so_what: string
}

export interface SectorRotationEntry {
  sector: string
  sector_label: string
  n_symbols: number
  ret_1d_avg: number
  ret_5d_avg: number | null
  rank: number
}

export interface Briefing {
  as_of_date: string | null
  headline: string
  // v2: the fuller morning-note paragraph (macro news + regime + sector
  // strength), plus which of the two shapes it took -- 'macro_news' when a
  // real event headline was found, 'fallback_regime_sector' when the
  // narrative had to degrade to regime+sector-only text.
  narrative: string
  narrative_basis: 'macro_news' | 'fallback_regime_sector'
  sector_rotation: SectorRotationEntry[]
  // Compact market-stats strip inputs (v2 homepage iteration): VIX level +
  // term-structure mood plus a plain up/down/flat breadth count across the
  // watchlist -- both already backing `narrative` above, just also exposed
  // as structured fields so the frontend can render a small stats strip
  // instead of re-parsing the narrative sentence.
  vix: number | null
  vix_term_slope: number | null
  breadth: { up: number; down: number; flat: number }
  guardrail: string
  market_lines: string[]
  symbol_lines: string[]
  scoreboard: ScoreboardSummary
}

// --- Post-market (end-of-day) wrap-up (F1) ------------------------------

export interface PostmarketMover {
  symbol: string
  sector: string | null
  change_pct: number
  close: number
  driver_headline: string | null
  driver_source: string | null
  driver_published_at: string | null
  driver_sentiment: number | null
}

export interface PostmarketWrap {
  as_of_date: string | null
  headline: string
  narrative: string
  dominant_regime: string | null
  vix: number | null
  vix_term_slope: number | null
  breadth: { up: number; down: number; flat: number }
  top_movers: PostmarketMover[]
  // reuses the same shape as SectorRotationEntry
  sector_strength: SectorRotationEntry[] | null
  notable: string[]
  data_sufficient: boolean
  insufficient_reason: string | null
}

export interface CockpitLevels {
  nday_high: number | null
  nday_low: number | null
  prev_high: number | null
  prev_low: number | null
  sma20: number | null
  sma50: number | null
}

export interface SellPutSuggestion {
  strike: number
  otm_pct: number
  gate_light: 'green' | 'red' | 'unknown'
  reason: string
  note: string
}

export interface VolumeSignal {
  ratio: number | null
  state: '放量' | '縮量' | '量能正常' | '資料不足'
  today_volume: number | null
  avg_volume_20d: number | null
}

export interface SectorLinkage {
  state: '脫離板塊獨走' | '跟隨板塊同步' | '不適用' | '資料不足'
  symbol_return: number | null
  peer_avg_return: number | null
  z: number | null
  note: string | null
}

export interface CockpitCard {
  symbol: string
  sector: string
  as_of_date: string | null
  price: number | null
  levels: CockpitLevels
  breakout_state: string
  regime: string
  top_news: {
    item_id: string
    headline: string
    sentiment_score: number | null
    importance: number | null
    published_at: string
  } | null
  sellput: SellPutSuggestion | null
  volume_signal: VolumeSignal
  sector_linkage: SectorLinkage
  // v2 (2026-07-14, Task D part 2): descriptive facts, never a direction
  // call. iv: ATM (50-delta) implied vol as a fraction (0.286 = 28.6%),
  // null if no iv_surface_daily row. dollar_volume: today's close x volume.
  // earnings_date: best-effort next earnings date (event_calendar DB-first,
  // Nasdaq network fallback -- see stockmoney.data.earnings_calendar), null
  // when genuinely unknown -- never fabricated.
  iv: number | null
  dollar_volume: number | null
  earnings_date: string | null
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
  newsFreshness: () => getJson<NewsFreshness>('/news-freshness'),
  leaderboard: () => getJson<LeaderboardEntry[]>('/leaderboard'),
  traderProfile: (id: string) => getJsonOrNull<TraderProfile>(`/traders/${encodeURIComponent(id)}`),
  watchlist: () => getJson<WatchlistResponse>('/watchlist'),
  pipelineHealth: () => getJson<PipelineHealthEntry[]>('/pipeline-health'),
  league: (window?: number) =>
    getJson<LeagueEntry[]>(`/league${window !== undefined ? `?window=${window}` : ''}`),
  leagueEquity: () => getJson<LeagueEquityEntry[]>('/league/equity'),
  leagueOverall: (window?: number) =>
    getJson<OverallStats>(`/league/overall${window !== undefined ? `?window=${window}` : ''}`),
  leagueTraining: (window?: number) =>
    getJson<LeagueTrainingEntry[]>(`/league/training${window !== undefined ? `?window=${window}` : ''}`),
  traders: () => getJson<Trader[]>('/traders'),
  tickerTraders: (symbol: string) =>
    getJsonOrNull<TickerTradersResponse>(`/ticker/${encodeURIComponent(symbol)}/traders`),
  divergence: (hours?: number) =>
    getJson<DivergenceRow[]>(`/divergence${hours !== undefined ? `?hours=${hours}` : ''}`),
  traderTrades: (limit?: number) =>
    getJson<TraderTradeFeedEntry[]>(`/trader-trades${limit !== undefined ? `?limit=${limit}` : ''}`),
  briefing: () => getJson<Briefing>('/briefing'),
  cockpit: () => getJson<CockpitCard[]>('/cockpit'),
  postmarket: () => getJson<PostmarketWrap>('/postmarket'),
  scoreboardSummary: () => getJson<ScoreboardSummary>('/scoreboard-summary'),
}
