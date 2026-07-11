import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import Tooltip from './Tooltip'

describe('Tooltip', () => {
  it('renders the label alongside the trigger content', () => {
    render(
      <Tooltip label="白話解釋">
        <span>信心</span>
      </Tooltip>
    )
    expect(screen.getByText('信心')).toBeInTheDocument()
    expect(screen.getByRole('tooltip')).toHaveTextContent('白話解釋')
  })
})
