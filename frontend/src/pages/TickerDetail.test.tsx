import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import TickerDetail from './TickerDetail'
import { api, type TickerDetail as TickerDetailData } from '../lib/api'

vi.mock('../lib/api')

// Default: no trader predictions (honest null / 404 state)
beforeEach(() => {
  vi.mocked(api.tickerTraders).mockResolvedValue(null)
})

const mockDetail: TickerDetailData = {
  symbol: 'SOXL', sector: 'semiconductor', trade_date: '2026-07-02', horizon: 5,
  label_end_date: '2026-07-09', regime: 2,
  proba: { down: 0.21, range: 0.59, up: 0.2 },
  predicted_direction: 'range', conviction: 0.59,
  entry_price: 174.82, target_price_up: 207, target_price_down: 143,
  feature_values: { realized_vol_20d: 2.59, adx_14: 16.21 },
  model_version: 'gmm-logistic-v1',
  backtest: {
    as_of_date: '2026-07-09', overall_n: 976, overall_accuracy: 0.39,
    overall_brier: 0.66, overall_sharpe: 0.92, ev_passed_n: 10,
    ev_passed_win_rate: 0.5, ev_blocked_n: 5, ev_blocked_win_rate: 0.4,
    ev_of_continuing_now: 0.0358,
  },
  history: [
    { prediction_id: 'p1', trade_date: '2026-07-02', predicted_direction: 'range', status: 'graded', outcome: 'win', label_end_date: '2026-07-09' },
  ],
  price_history: [
    { trade_date: '2026-07-01', close: 170 },
    { trade_date: '2026-07-02', close: 174.82 },
  ],
  catalyst: null,
}

function renderPage(symbol = 'SOXL') {
  return render(
    <MemoryRouter initialEntries={[`/ticker/${symbol}`]}>
      <Routes>
        <Route path="/ticker/:symbol" element={<TickerDetail />} />
      </Routes>
    </MemoryRouter>
  )
}

describe('TickerDetail', () => {
  it('renders the symbol header and key sections once loaded', async () => {
    vi.mocked(api.ticker).mockResolvedValue(mockDetail)
    renderPage()

    await waitFor(() => expect(screen.getByText('SOXL')).toBeInTheDocument())
    expect(screen.getByText('模型判斷機率')).toBeInTheDocument()
    expect(screen.getByText('為什麼：關鍵特徵值')).toBeInTheDocument()
    expect(screen.getByText('realized_vol_20d')).toBeInTheDocument()
  })

  it('shows an honest empty state for the catalyst panel when none exists', async () => {
    vi.mocked(api.ticker).mockResolvedValue(mockDetail)
    renderPage()

    await waitFor(() =>
      expect(screen.getByText(/尚無消息面資料/)).toBeInTheDocument()
    )
  })

  it('renders prediction history with graded outcome', async () => {
    vi.mocked(api.ticker).mockResolvedValue(mockDetail)
    renderPage()

    await waitFor(() => expect(screen.getByText('win')).toBeInTheDocument())
  })

  it('shows an error message when the API call fails', async () => {
    vi.mocked(api.ticker).mockRejectedValue(new Error('not found'))
    renderPage()

    await waitFor(() => expect(screen.getByText(/載入失敗/)).toBeInTheDocument())
  })

  it('shows an honest empty state for the per-trader section when no predictions exist', async () => {
    vi.mocked(api.ticker).mockResolvedValue(mockDetail)
    vi.mocked(api.tickerTraders).mockResolvedValue(null)
    renderPage()

    await waitFor(() => expect(screen.getByText('各交易員判斷')).toBeInTheDocument())
    expect(screen.getByText(/此標的尚無交易員判斷紀錄/)).toBeInTheDocument()
  })
})
