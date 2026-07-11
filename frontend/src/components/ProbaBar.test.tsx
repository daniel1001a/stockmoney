import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import ProbaBar from './ProbaBar'

describe('ProbaBar', () => {
  it('renders all three probability percentages', () => {
    render(<ProbaBar proba={{ up: 0.21, range: 0.59, down: 0.2 }} />)
    expect(screen.getByText('21%')).toBeInTheDocument()
    expect(screen.getByText('59%')).toBeInTheDocument()
    expect(screen.getByText('20%')).toBeInTheDocument()
  })

  it('renders the three direction labels', () => {
    render(<ProbaBar proba={{ up: 0.3, range: 0.4, down: 0.3 }} />)
    expect(screen.getByText('漲')).toBeInTheDocument()
    expect(screen.getByText('盤整')).toBeInTheDocument()
    expect(screen.getByText('跌')).toBeInTheDocument()
  })

  it('renders a single stacked bar with a tooltip in compact mode', () => {
    render(<ProbaBar proba={{ up: 0.21, range: 0.59, down: 0.2 }} compact />)
    expect(screen.queryByText('漲')).not.toBeInTheDocument()
    expect(screen.getByRole('tooltip')).toHaveTextContent('21%')
    expect(screen.getByRole('tooltip')).toHaveTextContent('59%')
    expect(screen.getByRole('tooltip')).toHaveTextContent('20%')
  })
})
