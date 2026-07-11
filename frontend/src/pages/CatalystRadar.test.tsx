import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import CatalystRadar from './CatalystRadar'
import { api, type CatalystsResponse } from '../lib/api'

vi.mock('../lib/api')

const populated: CatalystsResponse = {
  available: true,
  items: [
    {
      signal_id: 's1', symbol: 'NVDA', as_of_date: '2026-07-10',
      catalyst_summary: 'capex commentary accelerating',
      transmission_chain: 'hyperscaler capex -> GPU demand -> revenue',
      novelty_score: 0.8, sentiment_score: 0.5, priced_in_estimate: 0.2,
      source_refs: ['a1', 'p1'], model_version: 'catalyst-synthesis-sonnet-v1',
      available_at: '2026-07-10T00:00:00Z',
    },
  ],
}

function renderPage() {
  return render(
    <MemoryRouter>
      <CatalystRadar />
    </MemoryRouter>
  )
}

describe('CatalystRadar', () => {
  it('renders catalyst cards when data is available', async () => {
    vi.mocked(api.catalysts).mockResolvedValue(populated)
    renderPage()

    await waitFor(() => expect(screen.getByText('NVDA')).toBeInTheDocument())
    expect(screen.getByText('capex commentary accelerating')).toBeInTheDocument()
    expect(screen.getByText('2 則來源')).toBeInTheDocument()
  })

  it('shows an honest empty state when no catalysts exist yet', async () => {
    vi.mocked(api.catalysts).mockResolvedValue({ available: true, items: [] })
    renderPage()

    await waitFor(() =>
      expect(screen.getByText(/目前沒有新鮮的催化劑訊號/)).toBeInTheDocument()
    )
  })

  it('shows an error message when the API call fails', async () => {
    vi.mocked(api.catalysts).mockRejectedValue(new Error('boom'))
    renderPage()

    await waitFor(() => expect(screen.getByText(/載入失敗/)).toBeInTheDocument())
  })

  it('always shows an honest empty state for the known-events section (no calendar API exists)', async () => {
    vi.mocked(api.catalysts).mockResolvedValue(populated)
    renderPage()

    await waitFor(() => expect(screen.getByText('已知大事 / 未來排程消息')).toBeInTheDocument())
    expect(screen.getByText('尚未串接事件日曆')).toBeInTheDocument()
  })
})
