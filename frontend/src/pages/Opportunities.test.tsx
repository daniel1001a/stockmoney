import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Opportunities from './Opportunities'
import { api, type Opportunity } from '../lib/api'

vi.mock('../lib/api')

const mockOpportunity: Opportunity = {
  symbol: 'SOXL', sector: 'semiconductor', trade_date: '2026-07-02', horizon: 5,
  label_end_date: '2026-07-09', regime: 2,
  proba: { down: 0.21, range: 0.59, up: 0.2 },
  predicted_direction: 'range', conviction: 0.59,
  entry_price: 174.82, target_price_up: 207, target_price_down: 143,
  feature_values: { realized_vol_20d: 2.59 },
  model_version: 'gmm-logistic-v1',
  backtest: {
    as_of_date: '2026-07-09', overall_n: 976, overall_accuracy: 0.39,
    overall_brier: 0.66, overall_sharpe: 0.92, ev_passed_n: 10,
    ev_passed_win_rate: 0.5, ev_blocked_n: 5, ev_blocked_win_rate: 0.4,
    ev_of_continuing_now: 0.03,
  },
  catalyst_headline: null,
}

function renderPage() {
  return render(
    <MemoryRouter>
      <Opportunities />
    </MemoryRouter>
  )
}

describe('Opportunities', () => {
  it('shows a loading state, then renders opportunity cards', async () => {
    vi.mocked(api.opportunities).mockResolvedValue([mockOpportunity])
    renderPage()

    expect(screen.getByText('載入中…')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('SOXL')).toBeInTheDocument())
    expect(screen.getByText('semiconductor · regime 2')).toBeInTheDocument()
  })

  it('shows an honest empty state when there is no cached prediction data', async () => {
    vi.mocked(api.opportunities).mockResolvedValue([])
    renderPage()

    await waitFor(() =>
      expect(screen.getByText(/目前沒有任何標的的預測快取/)).toBeInTheDocument()
    )
  })

  it('shows a placeholder instead of a headline when no catalyst data exists', async () => {
    vi.mocked(api.opportunities).mockResolvedValue([mockOpportunity])
    renderPage()

    await waitFor(() => expect(screen.getByText('尚無消息面催化劑資料')).toBeInTheDocument())
  })

  it('shows an error message when the API call fails', async () => {
    vi.mocked(api.opportunities).mockRejectedValue(new Error('network down'))
    renderPage()

    await waitFor(() => expect(screen.getByText(/載入失敗/)).toBeInTheDocument())
  })
})
