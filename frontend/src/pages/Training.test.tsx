import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Training from './Training'
import { api, type LeagueTrainingEntry, type TraderStats } from '../lib/api'

vi.mock('../lib/api')

const stats = (over: Partial<TraderStats> = {}): TraderStats => ({
  n_graded: 8, n_directional: 8, hit_rate: 0.5, brier: 0.2, avg_pnl: 0.01, cum_pnl: 0.1,
  high_conviction_threshold: 0.6, high_conviction_n: 2, high_conviction_precision: 0.5,
  cum_option_pnl: 300, ...over,
})

const trainingEntry = (over: Partial<LeagueTrainingEntry> = {}): LeagueTrainingEntry => ({
  trader_id: 'reversion', name: 'Reversion (反轉派)', philosophy: 'fade extremes', active: true,
  overall: stats(), rolling: { window: 20, ...stats() }, by_regime: { '0': stats({ n_graded: 5 }) },
  win_rate_series: [
    { trade_date: '2026-07-01', n: 1, hit_rate: 1 },
    { trade_date: '2026-07-03', n: 2, hit_rate: 0.5 },
  ],
  proposals: [
    { proposal_id: 'p1', trader_id: 'reversion', from_version: 'v1', to_version: null,
      rationale: '納入成交量確認', status: 'proposed', reviewed_by: null, reviewed_date: null },
  ],
  method_versions: [{ method_version: 'reversion:rsi-meanrev-v1', effective_date: '2026-07-17', status: 'active' }],
  ...over,
})

describe('Training', () => {
  it('renders a trader scorecard, win-rate trend and its self-improvement proposal', async () => {
    vi.mocked(api.leagueTraining).mockResolvedValue([trainingEntry()])
    render(<MemoryRouter><Training /></MemoryRouter>)
    await waitFor(() => expect(screen.getByText('Reversion (反轉派)')).toBeInTheDocument())
    expect(screen.getByText('訓練表現')).toBeInTheDocument()
    expect(screen.getByText('納入成交量確認')).toBeInTheDocument()
    expect(screen.getByText('提議中')).toBeInTheDocument()
  })

  it('renders honest empty states for a trader with no graded data or proposals', async () => {
    vi.mocked(api.leagueTraining).mockResolvedValue([
      trainingEntry({ trader_id: 'sentiment', name: 'Sentiment (情緒派)',
        by_regime: {}, win_rate_series: [], proposals: [] }),
    ])
    render(<MemoryRouter><Training /></MemoryRouter>)
    await waitFor(() => expect(screen.getByText('Sentiment (情緒派)')).toBeInTheDocument())
    expect(screen.getByText(/尚無改進提案/)).toBeInTheDocument()
  })
})
