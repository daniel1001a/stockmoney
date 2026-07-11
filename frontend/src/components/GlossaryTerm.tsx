import type { ReactNode } from 'react'
import { GLOSSARY } from '../lib/glossary'
import Tooltip from './Tooltip'

interface Props {
  term: string
  children: ReactNode
  className?: string
}

// Wraps a jargon label with a plain-language tooltip looked up from
// glossary.ts. Falls back to rendering children plainly if the term isn't
// in the glossary (never crashes on an unknown key).
export default function GlossaryTerm({ term, children, className }: Props) {
  const explanation = GLOSSARY[term]
  if (!explanation) {
    return <span className={className}>{children}</span>
  }
  return (
    <Tooltip label={explanation}>
      <span className={`border-b border-dotted border-neutral-500 cursor-help ${className ?? ''}`}>{children}</span>
    </Tooltip>
  )
}
