import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Arena from './Arena'
import { api, type LeaderboardEntry, type TraderTradeFeedEntry } from '../lib/api'

vi.mock('../lib/api')

const entry = (rank: number, id: string, name: string, ret: number): LeaderboardEntry => ({
  rank, trader_id: id, name, philosophy: 'p', active: true, starting_capital: 25000,
  realized_pnl: ret * 25000, unrealized_pnl: 0, equity: 25000 * (1 + ret),
  total_return_pct: ret, realized_return_pct: ret, n_closed: 10, n_open: 2,
  trade_win_rate: 0.5, best_trade: 500, worst_trade: -300, hit_rate: 0.5, brier: 0.2,
  n_directional: 8, option_win_rate: 0.45, avg_option_pnl: -0.1,
})

const tradeRow = (overrides: Partial<TraderTradeFeedEntry> = {}): TraderTradeFeedEntry => ({
  trade_id: 't1', trader_id: 'momentum', trader_name: 'Momentum (動能派)', philosophy: 'p',
  symbol: 'MSFT', option_right: 'call', side: 'long', strike: 373.3, expiry_date: '2026-07-07',
  contracts: 1, entry_at: '2026-07-01T00:00:00Z', entry_underlying: 370, entry_premium: 5.2,
  exit_at: null, exit_underlying: null, exit_premium: null, realized_pnl: null,
  status: 'open', thesis: 'AI 資本支出加速', exit_reason: null,
  ...overrides,
})

describe('Arena', () => {
  it('renders the contest rules and a return-ranked leaderboard', async () => {
    vi.mocked(api.leaderboard).mockResolvedValue([
      entry(1, 'momentum', 'Momentum (動能派)', 0.14),
      entry(2, 'analyst', 'Analyst (消息派)', -0.05),
    ])
    vi.mocked(api.divergence).mockResolvedValue([])
    vi.mocked(api.traderTrades).mockResolvedValue([])

    render(<MemoryRouter><Arena /></MemoryRouter>)

    await waitFor(() => expect(screen.getByText('排行榜')).toBeInTheDocument())
    expect(screen.getByText('比賽規則')).toBeInTheDocument()
    expect(screen.getByText('Momentum (動能派)')).toBeInTheDocument()
    // realized return rendered as a signed percentage
    expect(screen.getByText('+14.0%')).toBeInTheDocument()
    expect(screen.getByText('-5.0%')).toBeInTheDocument()
  })

  it('renders the live board with professional option notation and P&L', async () => {
    vi.mocked(api.leaderboard).mockResolvedValue([entry(1, 'momentum', 'Momentum (動能派)', 0.14)])
    vi.mocked(api.divergence).mockResolvedValue([])
    vi.mocked(api.traderTrades).mockResolvedValue([
      tradeRow(),
      tradeRow({
        trade_id: 't2', status: 'closed', side: 'short', option_right: 'put', symbol: 'AAPL',
        strike: 200, expiry_date: '2026-07-10', exit_at: '2026-07-05T00:00:00Z',
        exit_underlying: 198, exit_premium: 1.1, realized_pnl: 250, exit_reason: '達停利目標',
      }),
    ])

    render(<MemoryRouter><Arena /></MemoryRouter>)

    await waitFor(() => expect(screen.getByText('交易動態 (Live Board)')).toBeInTheDocument())
    expect(screen.getByText('MSFT 373.3 Call 7/7')).toBeInTheDocument()
    expect(screen.getByText('AAPL 200 Put 7/10')).toBeInTheDocument()
    expect(screen.getByText('持倉中')).toBeInTheDocument()
    expect(screen.getByText('已平倉')).toBeInTheDocument()
    expect(screen.getByText('+$250')).toBeInTheDocument()
    expect(screen.getByText('達停利目標', { exact: false })).toBeInTheDocument()
  })
})
