import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Opportunities from './Opportunities'
import { api, type Opportunity, type MarketSummary, type Quote } from '../lib/api'

vi.mock('../lib/api')

const opp = (symbol: string, conviction: number, direction: Opportunity['predicted_direction']): Opportunity => ({
  symbol, sector: 'semiconductor', trade_date: '2026-07-10', horizon: 5,
  label_end_date: '2026-07-17', regime: 2, regime_label: '高波動趨勢盤',
  thesis: `高波動趨勢盤格局下,模型偏向看漲,信心 ${Math.round(conviction * 100)}%。`,
  proba: { down: 0.2, range: 0.2, up: 0.6 }, predicted_direction: direction, conviction,
  actionable: direction !== 'range',
  directional_conviction: direction === 'range' ? null : conviction,
  regime_is_trending: true,
  entry_price: 178, target_price_up: 190, target_price_down: 166,
  feature_values: { realized_vol_20d: 0.5 }, model_version: 'gmm-logistic-v2',
  backtest: {
    as_of_date: '2026-07-10', overall_n: 500, overall_accuracy: 0.48, overall_brier: 0.23,
    overall_sharpe: 0.4, ev_passed_n: 50, ev_passed_win_rate: 0.5, ev_blocked_n: 80,
    ev_blocked_win_rate: 0.4, ev_of_continuing_now: 0.01,
  },
  catalyst_headline: null,
  top_news: null,
})

const market: MarketSummary = {
  as_of_date: '2026-07-10', n_symbols: 2, direction_counts: { up: 1, down: 1, range: 0 },
  regime_counts: { 高波動趨勢盤: 2 }, dominant_regime: '高波動趨勢盤', avg_conviction: 0.65,
  vix: 12.5, vix_term_slope: 1.2,
  analyst_sentiment: { n_symbols_covered: 2, bullish: 1, neutral: 1, bearish: 0 },
  top_gainers: [{ symbol: 'NVDA', close: 178, change_pct: 0.06 }],
  top_losers: [{ symbol: 'AMD', close: 170, change_pct: -0.03 }],
}

describe('Opportunities', () => {
  it('renders the market strip, top picks and the sortable watchlist', async () => {
    vi.mocked(api.opportunities).mockResolvedValue([opp('NVDA', 0.77, 'up'), opp('AMD', 0.55, 'down')])
    vi.mocked(api.marketSummary).mockResolvedValue(market)
    vi.mocked(api.quotes).mockResolvedValue({})

    render(<MemoryRouter><Opportunities /></MemoryRouter>)

    await waitFor(() => expect(screen.getByRole('table')).toBeInTheDocument())
    // human strength label appears (no bare "regime 0", no up/down direction word)
    expect(screen.getAllByText('高波動趨勢盤').length).toBeGreaterThan(0)
    // both the picks section and the watchlist show the symbols
    expect(screen.getAllByText('NVDA').length).toBeGreaterThan(0)
    // both are directional -> 2 picks featured
    expect(screen.getByText('本日精選 · 2 個方向性機會')).toBeInTheDocument()
    expect(screen.getByText('核心觀察清單')).toBeInTheDocument()
  })

  it('shows a live price + change% badge once a quote is available', async () => {
    vi.mocked(api.opportunities).mockResolvedValue([opp('NVDA', 0.77, 'up')])
    vi.mocked(api.marketSummary).mockResolvedValue(market)
    const quote: Quote = { price: 185.5, prev_close: 178.0, change_pct: 0.0421, as_of: '2026-07-13T14:00:00Z' }
    vi.mocked(api.quotes).mockResolvedValue({ NVDA: quote })

    render(<MemoryRouter><Opportunities /></MemoryRouter>)

    // NVDA appears in both the picks section and the watchlist table -> the
    // live-price badge legitimately renders twice.
    await waitFor(() => expect(screen.getAllByText('$185.50').length).toBeGreaterThan(0))
    expect(screen.getAllByText('+4.2%').length).toBeGreaterThan(0)
  })

  it('excludes high-conviction range calls from the featured picks', async () => {
    // A range call with HIGHER conviction than the directional call must not be
    // featured -- 精選 is money-making directional edge, not raw confidence.
    vi.mocked(api.opportunities).mockResolvedValue([opp('AMD', 0.55, 'up'), opp('MSFT', 0.92, 'range')])
    vi.mocked(api.marketSummary).mockResolvedValue(market)
    vi.mocked(api.quotes).mockResolvedValue({})

    render(<MemoryRouter><Opportunities /></MemoryRouter>)

    await waitFor(() => expect(screen.getByRole('table')).toBeInTheDocument())
    // Only the one directional call is featured.
    expect(screen.getByText('本日精選 · 1 個方向性機會')).toBeInTheDocument()
    // The range symbol still appears (analysed, in the watchlist), just not featured.
    expect(screen.getAllByText('MSFT').length).toBeGreaterThan(0)
    expect(screen.getAllByText('盤整 · 無方向').length).toBeGreaterThan(0)
  })
})
