import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import Sparkline from './Sparkline'

describe('Sparkline', () => {
  it('shows the empty message with fewer than 2 points', () => {
    render(<Sparkline points={[{ value: 1 }]} emptyMessage="not enough data" />)
    expect(screen.getByText('not enough data')).toBeInTheDocument()
  })

  it('shows the empty message with zero points', () => {
    render(<Sparkline points={[]} emptyMessage="nothing here" />)
    expect(screen.getByText('nothing here')).toBeInTheDocument()
  })

  it('renders an svg polyline for 2+ points', () => {
    const { container } = render(
      <Sparkline points={[{ value: 1 }, { value: 2 }, { value: 1.5 }]} />
    )
    expect(container.querySelector('svg')).toBeInTheDocument()
    expect(container.querySelector('polyline')).toBeInTheDocument()
  })

  it('does not crash when all values are identical (zero range)', () => {
    const { container } = render(<Sparkline points={[{ value: 5 }, { value: 5 }, { value: 5 }]} />)
    expect(container.querySelector('polyline')).toBeInTheDocument()
  })
})
