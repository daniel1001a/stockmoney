import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import League from './League'
import { api, type LeagueEntry } from '../lib/api'

vi.mock('../lib/api')

const mockEntry: LeagueEntry = {
  trader_id: 'chartist-v1',
  name: 'Chartist v1',
  philosophy: 'Technical momentum',
  active: true,
  overall: {
    n_graded: 5, n_directional: 5, hit_rate: 0.6, brier: 0.24,
    avg_pnl: 0.02, cum_pnl: 0.1,
    high_conviction_threshold: 0.6, high_conviction_n: 3, high_conviction_precision: 0.67,
  },
  rolling: {
    window: 20, n_graded: 5, n_directional: 5, hit_rate: 0.6, brier: 0.24,
    avg_pnl: 0.02, cum_pnl: 0.1,
    high_conviction_threshold: 0.6, high_conviction_n: 3, high_conviction_precision: 0.67,
  },
  by_regime: {},
}

describe('League', () => {
  it('renders trader name once league data loads', async () => {
    vi.mocked(api.league).mockResolvedValue([mockEntry])
    vi.mocked(api.divergence).mockResolvedValue([])
    render(<MemoryRouter><League /></MemoryRouter>)
    await waitFor(() => expect(screen.getByText('Chartist v1')).toBeInTheDocument())
  })

  it('renders honest empty state when no traders have records', async () => {
    vi.mocked(api.league).mockResolvedValue([])
    vi.mocked(api.divergence).mockResolvedValue([])
    render(<MemoryRouter><League /></MemoryRouter>)
    await waitFor(() =>
      expect(screen.getByText('尚無交易員參賽紀錄')).toBeInTheDocument()
    )
  })

  it('renders honest empty divergence state when no rows', async () => {
    vi.mocked(api.league).mockResolvedValue([])
    vi.mocked(api.divergence).mockResolvedValue([])
    render(<MemoryRouter><League /></MemoryRouter>)
    await waitFor(() =>
      expect(screen.getByText('近期暫無分歧紀錄')).toBeInTheDocument()
    )
  })

  it('shows an error message when league API fails', async () => {
    vi.mocked(api.league).mockRejectedValue(new Error('fetch failed'))
    vi.mocked(api.divergence).mockResolvedValue([])
    render(<MemoryRouter><League /></MemoryRouter>)
    await waitFor(() => expect(screen.getByText(/載入失敗/)).toBeInTheDocument())
  })
})
