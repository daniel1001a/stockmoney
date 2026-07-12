import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Arena from './Arena'
import { api, type LeaderboardEntry } from '../lib/api'

vi.mock('../lib/api')

const entry = (rank: number, id: string, name: string, ret: number): LeaderboardEntry => ({
  rank, trader_id: id, name, philosophy: 'p', active: true, starting_capital: 25000,
  realized_pnl: ret * 25000, unrealized_pnl: 0, equity: 25000 * (1 + ret),
  total_return_pct: ret, realized_return_pct: ret, n_closed: 10, n_open: 2,
  trade_win_rate: 0.5, best_trade: 500, worst_trade: -300, hit_rate: 0.5, brier: 0.2,
  n_directional: 8,
})

describe('Arena', () => {
  it('renders the contest rules and a return-ranked leaderboard', async () => {
    vi.mocked(api.leaderboard).mockResolvedValue([
      entry(1, 'momentum', 'Momentum (動能派)', 0.14),
      entry(2, 'analyst', 'Analyst (消息派)', -0.05),
    ])
    vi.mocked(api.positions).mockResolvedValue([])
    vi.mocked(api.divergence).mockResolvedValue([])

    render(<MemoryRouter><Arena /></MemoryRouter>)

    await waitFor(() => expect(screen.getByText('排行榜')).toBeInTheDocument())
    expect(screen.getByText('比賽規則')).toBeInTheDocument()
    expect(screen.getByText('Momentum (動能派)')).toBeInTheDocument()
    // realized return rendered as a signed percentage
    expect(screen.getByText('+14.0%')).toBeInTheDocument()
    expect(screen.getByText('-5.0%')).toBeInTheDocument()
  })
})
