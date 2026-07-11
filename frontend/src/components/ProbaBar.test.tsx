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
})
