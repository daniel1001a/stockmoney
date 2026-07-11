import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import GlossaryTerm from './GlossaryTerm'

describe('GlossaryTerm', () => {
  it('wraps a known term with its tooltip explanation', () => {
    render(<GlossaryTerm term="conviction">信心</GlossaryTerm>)
    expect(screen.getByText('信心')).toBeInTheDocument()
    expect(screen.getByRole('tooltip')).toHaveTextContent(/機率分佈中最高的那一項/)
  })

  it('renders children plainly for an unknown term instead of crashing', () => {
    render(<GlossaryTerm term="not_a_real_term">未知術語</GlossaryTerm>)
    expect(screen.getByText('未知術語')).toBeInTheDocument()
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })
})
