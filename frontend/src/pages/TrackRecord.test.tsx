import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import TrackRecord from './TrackRecord'
import { api, type PredictionsOverview } from '../lib/api'

vi.mock('../lib/api')

const populated: PredictionsOverview = {
  rolling_win_rate: [
    { label_end_date: '2026-07-09', rolling_win_rate: 0.6 },
    { label_end_date: '2026-07-10', rolling_win_rate: 0.55 },
  ],
  outcome_counts: { win: 6, loss: 4 },
  recent: [
    {
      prediction_id: 'p1', trade_date: '2026-07-02', symbol: 'AAPL', sector: 'big_tech',
      regime: 2, predicted_direction: 'range', status: 'graded', outcome: 'win',
      label_end_date: '2026-07-09',
    },
  ],
}

const empty: PredictionsOverview = { rolling_win_rate: [], outcome_counts: {}, recent: [] }

function renderPage() {
  return render(
    <MemoryRouter>
      <TrackRecord />
    </MemoryRouter>
  )
}

describe('TrackRecord', () => {
  it('renders win/loss counts and the recent predictions table', async () => {
    vi.mocked(api.predictions).mockResolvedValue(populated)
    renderPage()

    await waitFor(() => expect(screen.getByText('6')).toBeInTheDocument())
    expect(screen.getByText('4')).toBeInTheDocument()
    expect(screen.getByText('AAPL')).toBeInTheDocument()
  })

  it('shows honest empty states when there is no data yet', async () => {
    vi.mocked(api.predictions).mockResolvedValue(empty)
    renderPage()

    await waitFor(() => expect(screen.getByText('尚無已驗證的方向性預測')).toBeInTheDocument())
    expect(screen.getByText('尚無已驗證紀錄')).toBeInTheDocument()
    expect(screen.getByText('尚無任何預測紀錄')).toBeInTheDocument()
  })

  it('shows an error message when the API call fails', async () => {
    vi.mocked(api.predictions).mockRejectedValue(new Error('boom'))
    renderPage()

    await waitFor(() => expect(screen.getByText(/載入失敗/)).toBeInTheDocument())
  })
})
