import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Arena from './Arena'
import { api, type LeaderboardEntry, type LeagueEquityEntry, type TraderTradeFeedEntry, type OverallStats } from '../lib/api'

vi.mock('../lib/api')

const entry = (rank: number, id: string, name: string, ret: number, overrides: Partial<LeaderboardEntry> = {}): LeaderboardEntry => ({
  rank, trader_id: id, name, philosophy: 'p', active: true, starting_capital: 25000,
  realized_pnl: ret * 25000, unrealized_pnl: 0, equity: 25000 * (1 + ret),
  total_return_pct: ret, realized_return_pct: ret, n_closed: 10, n_open: 2,
  trade_win_rate: 0.5, best_trade: 500, worst_trade: -300, hit_rate: 0.5, brier: 0.2,
  n_directional: 8, option_win_rate: 0.45, avg_option_pnl: -0.1,
  cum_option_pnl: ret * 25000, n_graded: 8,
  profitable_rate: 0.45, expected_value: -0.1, data_sufficient: true,
  ...overrides,
})

const tradeRow = (overrides: Partial<TraderTradeFeedEntry> = {}): TraderTradeFeedEntry => ({
  trade_id: 't1', trader_id: 'momentum', trader_name: 'Momentum (動能派)', philosophy: 'p',
  symbol: 'MSFT', option_right: 'call', side: 'long', strike: 373.3, expiry_date: '2026-07-07',
  contracts: 1, entry_at: '2026-07-01T00:00:00Z', entry_underlying: 370, entry_premium: 5.2,
  exit_at: null, exit_underlying: null, exit_premium: null, realized_pnl: null,
  status: 'open', thesis: 'AI 資本支出加速', exit_reason: null,
  ...overrides,
})

const equityEntry = (id: string, name: string, points: LeagueEquityEntry['points']): LeagueEquityEntry => ({
  trader_id: id, name, philosophy: 'p', active: true, points,
})

const overallStats = (over: Partial<OverallStats> = {}): OverallStats => ({
  window: null, n_traders: 5, n_graded: 40, n_directional: 32, hit_rate: 0.5, brier: 0.24,
  avg_pnl: 0.02, cum_pnl: 0.6, n_option_graded: 30, option_win_rate: 0.4, avg_option_pnl: -0.05,
  cum_option_pnl: -300, ...over,
})

describe('Arena', () => {
  it('renders the contest rules and a return-ranked leaderboard', async () => {
    vi.mocked(api.leaderboard).mockResolvedValue([
      entry(1, 'momentum', 'Momentum (動能派)', 0.14),
      entry(2, 'analyst', 'Analyst (消息派)', -0.05),
    ])
    vi.mocked(api.leagueEquity).mockResolvedValue([
      equityEntry('momentum', 'Momentum (動能派)', [
        { trade_date: '2026-07-01', symbol: 'MSFT', direction: 'up', pnl: 0.1, cum_pnl: 0.1, option_pnl: 200, cum_option_pnl: 200 },
        { trade_date: '2026-07-03', symbol: 'AAPL', direction: 'up', pnl: 0.05, cum_pnl: 0.15, option_pnl: 100, cum_option_pnl: 300 },
      ]),
      equityEntry('analyst', 'Analyst (消息派)', []),
    ])
    vi.mocked(api.divergence).mockResolvedValue([])
    vi.mocked(api.traderTrades).mockResolvedValue([])
    vi.mocked(api.leagueOverall).mockResolvedValue(overallStats())

    render(<MemoryRouter><Arena /></MemoryRouter>)

    await waitFor(() => expect(screen.getByText('排行榜')).toBeInTheDocument())
    expect(screen.getByText('比賽規則')).toBeInTheDocument()
    expect(screen.getAllByText('Momentum (動能派)').length).toBeGreaterThan(0)
    // realized return rendered as a signed percentage
    expect(screen.getByText('+14.0%')).toBeInTheDocument()
    expect(screen.getByText('-5.0%')).toBeInTheDocument()
    // equity curve + compact standings section
    expect(screen.getByText('資金曲線與戰績')).toBeInTheDocument()
    expect(screen.getByText('+$3,500')).toBeInTheDocument() // cum_option_pnl for momentum in StandingsCompact
  })

  it('shows the terminology-split scorecard and multi-horizon breakdown (issue #7 P1)', async () => {
    vi.mocked(api.leaderboard).mockResolvedValue([
      entry(1, 'momentum', 'Momentum (動能派)', 0.14, {
        profitable_rate: 0.45, expected_value: -0.123, data_sufficient: true,
        horizons: {
          '1': { n_graded: 12, n_directional: 12, hit_rate: 0.6, brier: 0.2, avg_pnl: 0.01, cum_pnl: 0.1,
                 high_conviction_threshold: 0.6, high_conviction_n: 5, high_conviction_precision: 0.6,
                 profitable_rate: 0.5, expected_value: 0.02, data_sufficient: false },
          '5': { n_graded: 0, n_directional: 0, hit_rate: null, brier: null, avg_pnl: null, cum_pnl: 0,
                 high_conviction_threshold: 0.6, high_conviction_n: 0, high_conviction_precision: null,
                 data_sufficient: false },
          '21': { n_graded: 0, n_directional: 0, hit_rate: null, brier: null, avg_pnl: null, cum_pnl: 0,
                  high_conviction_threshold: 0.6, high_conviction_n: 0, high_conviction_precision: null,
                  data_sufficient: false },
        },
      }),
      entry(2, 'analyst', 'Analyst (消息派)', -0.05, { data_sufficient: false }),
    ])
    vi.mocked(api.leagueEquity).mockResolvedValue([])
    vi.mocked(api.divergence).mockResolvedValue([])
    vi.mocked(api.traderTrades).mockResolvedValue([])
    vi.mocked(api.leagueOverall).mockResolvedValue(overallStats())

    render(<MemoryRouter><Arena /></MemoryRouter>)

    await waitFor(() => expect(screen.getByText('評分視野(短/中/長)')).toBeInTheDocument())
    // StandingsCompact now shows 賺錢率/期望值 alongside 方向命中率, and flags
    // an insufficient-sample trader instead of implying its rank is real.
    expect(screen.getAllByText('45%').length).toBeGreaterThan(0)  // profitable_rate
    expect(screen.getAllByText('資料不足').length).toBeGreaterThan(0)
    // The 1/5/21-day horizon breakdown section rendered real numbers for the
    // graded horizon and an honest "資料不足" for the still-empty ones.
    expect(screen.getByText('1 日(命中率 / 賺錢率 / 期望值)')).toBeInTheDocument()
  })

  it('shows an honest empty state when no trader has graded predictions yet', async () => {
    vi.mocked(api.leaderboard).mockResolvedValue([entry(1, 'momentum', 'Momentum (動能派)', 0.14)])
    vi.mocked(api.leagueEquity).mockResolvedValue([
      equityEntry('momentum', 'Momentum (動能派)', []),
      equityEntry('analyst', 'Analyst (消息派)', []),
    ])
    vi.mocked(api.divergence).mockResolvedValue([])
    vi.mocked(api.traderTrades).mockResolvedValue([])
    vi.mocked(api.leagueOverall).mockResolvedValue(overallStats())

    render(<MemoryRouter><Arena /></MemoryRouter>)

    await waitFor(() => expect(screen.getByText('資金曲線與戰績')).toBeInTheDocument())
    expect(screen.getByText('資料不足,尚無足夠已結算紀錄')).toBeInTheDocument()
  })

  it('renders the live board with professional option notation and P&L', async () => {
    vi.mocked(api.leaderboard).mockResolvedValue([entry(1, 'momentum', 'Momentum (動能派)', 0.14)])
    vi.mocked(api.leagueEquity).mockResolvedValue([])
    vi.mocked(api.divergence).mockResolvedValue([])
    vi.mocked(api.traderTrades).mockResolvedValue([
      tradeRow(),
      tradeRow({
        trade_id: 't2', status: 'closed', side: 'short', option_right: 'put', symbol: 'AAPL',
        strike: 200, expiry_date: '2026-07-10', exit_at: '2026-07-05T00:00:00Z',
        exit_underlying: 198, exit_premium: 1.1, realized_pnl: 250, exit_reason: '達停利目標',
      }),
    ])
    vi.mocked(api.leagueOverall).mockResolvedValue(overallStats())

    render(<MemoryRouter><Arena /></MemoryRouter>)

    await waitFor(() => expect(screen.getByText('交易動態 (Live Board)')).toBeInTheDocument())
    expect(screen.getByText('MSFT 373.3 Call 7/7')).toBeInTheDocument()
    expect(screen.getByText('AAPL 200 Put 7/10')).toBeInTheDocument()
    expect(screen.getByText('持倉中')).toBeInTheDocument()
    // Closed trade with realized_pnl > 0 renders the win outcome-state badge.
    expect(screen.getByText('獲利平倉')).toBeInTheDocument()
    expect(screen.getByText('+$250')).toBeInTheDocument()
    expect(screen.getByText('達停利目標', { exact: false })).toBeInTheDocument()
  })
})
