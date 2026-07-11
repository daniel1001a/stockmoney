import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import PositionsRisk from './PositionsRisk'
import { api, type PositionRisk } from '../lib/api'

vi.mock('../lib/api')

const mockPosition: PositionRisk = {
  position_id: 'pos1', symbol: 'NVDA', option_right: 'call', side: 'long',
  entry_date: '2026-07-01', entry_underlying_price: 175.0, entry_premium: 8.0,
  current_underlying_price: 180.0, as_of_date: '2026-07-09', light: 'yellow',
  triggers: [{ kind: 'price_stop', light: 'yellow', detail: 'approaching -2 IV stddev' }],
  notes: ['dynamic EV skipped: symbol not in production scope'],
}

function renderPage() {
  return render(
    <MemoryRouter>
      <PositionsRisk />
    </MemoryRouter>
  )
}

describe('PositionsRisk', () => {
  it('renders a position card with its risk light, triggers, and notes', async () => {
    vi.mocked(api.positions).mockResolvedValue([mockPosition])
    renderPage()

    await waitFor(() => expect(screen.getByText('NVDA')).toBeInTheDocument())
    expect(screen.getByText(/approaching -2 IV stddev/)).toBeInTheDocument()
    expect(screen.getByText(/dynamic EV skipped/)).toBeInTheDocument()
  })

  it('shows an honest empty state when there are no open positions', async () => {
    vi.mocked(api.positions).mockResolvedValue([])
    renderPage()

    await waitFor(() => expect(screen.getByText(/目前沒有開倉部位/)).toBeInTheDocument())
  })

  it('shows an error message when the API call fails', async () => {
    vi.mocked(api.positions).mockRejectedValue(new Error('boom'))
    renderPage()

    await waitFor(() => expect(screen.getByText(/載入失敗/)).toBeInTheDocument())
  })
})
