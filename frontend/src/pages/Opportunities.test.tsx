import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Opportunities from './Opportunities'
import { api, type Opportunity, type MarketSummary } from '../lib/api'

vi.mock('../lib/api')

const opp = (symbol: string, conviction: number, direction: Opportunity['predicted_direction']): Opportunity => ({
  symbol, sector: 'semiconductor', trade_date: '2026-07-10', horizon: 5,
  label_end_date: '2026-07-17', regime: 2, regime_label: '高波動趨勢',
  thesis: `高波動趨勢格局下,模型偏向看漲,信心 ${Math.round(conviction * 100)}%。`,
  proba: { down: 0.2, range: 0.2, up: 0.6 }, predicted_direction: direction, conviction,
  entry_price: 178, target_price_up: 190, target_price_down: 166,
  feature_values: { realized_vol_20d: 0.5 }, model_version: 'gmm-logistic-v2',
  backtest: {
    as_of_date: '2026-07-10', overall_n: 500, overall_accuracy: 0.48, overall_brier: 0.23,
    overall_sharpe: 0.4, ev_passed_n: 50, ev_passed_win_rate: 0.5, ev_blocked_n: 80,
    ev_blocked_win_rate: 0.4, ev_of_continuing_now: 0.01,
  },
  catalyst_headline: null,
})

const market: MarketSummary = {
  as_of_date: '2026-07-10', n_symbols: 2, direction_counts: { up: 1, down: 1, range: 0 },
  regime_counts: { 高波動趨勢: 2 }, dominant_regime: '高波動趨勢', avg_conviction: 0.65,
  vix: 12.5, vix_term_slope: 1.2, top_gainers: [{ symbol: 'NVDA', close: 178, change_pct: 0.06 }],
  top_losers: [{ symbol: 'AMD', close: 170, change_pct: -0.03 }],
}

describe('Opportunities', () => {
  it('renders the market strip, top picks and the sortable watchlist', async () => {
    vi.mocked(api.opportunities).mockResolvedValue([opp('NVDA', 0.77, 'up'), opp('AMD', 0.55, 'down')])
    vi.mocked(api.marketSummary).mockResolvedValue(market)

    render(<MemoryRouter><Opportunities /></MemoryRouter>)

    await waitFor(() => expect(screen.getByRole('table')).toBeInTheDocument())
    // human strength label appears (no bare "regime 0", no up/down direction word)
    expect(screen.getAllByText('高波動趨勢').length).toBeGreaterThan(0)
    // both the picks section and the watchlist show the symbols
    expect(screen.getAllByText('NVDA').length).toBeGreaterThan(0)
    expect(screen.getByText('本日精選 · 最有信心的 5 檔')).toBeInTheDocument()
    expect(screen.getByText('核心觀察清單')).toBeInTheDocument()
  })
})
