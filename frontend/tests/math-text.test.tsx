import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { MathText } from '@/components/solutions/math-text'

/**
 * The screen where a student checks Lyra's reading against their own sheet was the one
 * screen printing raw text, so an exponent the PDF had flattened stayed flattened. These
 * cover what that fix must not break: a statement that contains no mathematics, and one
 * whose mathematics is malformed.
 */
describe('MathText', () => {
  it('typesets an equation', () => {
    const { container } = render(<MathText>{'Compute $x(t) = e^{-2t}u(t-3)$.'}</MathText>)

    expect(container.querySelector('.katex')).not.toBeNull()
  })

  it('leaves prose alone', () => {
    render(<MathText>Explain why the system is stable.</MathText>)

    expect(screen.getByText('Explain why the system is stable.')).toBeInTheDocument()
  })

  it('keeps the rest of a statement when one equation is malformed', () => {
    // Statements are transcribed from a PDF, so a stray brace is a question of when
    // rather than whether. Blanking the row the student is meant to be checking would
    // be the worst possible response to it.
    const { container } = render(<MathText>{'Part (a): $\\frac{1$ and then stop.'}</MathText>)

    expect(container.textContent).toContain('and then stop.')
  })

  it('keeps a long expression on the line it was written on', () => {
    // The chat promotes a long inline expression to its own centred block, which is right
    // for an answer and wrong for a list of five sub-parts: one of them would sit centred
    // on its own line while its four siblings stayed inline.
    const { container } = render(
      <MathText inline>{'$x(t) = \\sin(t) [u(t + 1) - u(t - 1)]$'}</MathText>,
    )

    expect(container.querySelector('.katex-display')).toBeNull()
    expect(container.querySelector('.katex')).not.toBeNull()
  })

  it('renders a single-line row without block wrappers', () => {
    // A sub-part sits in a truncated flex row. A paragraph element there would take the
    // row's ellipsis with it.
    const { container } = render(<MathText inline>{'$x(t) = te^{-t}u(t)$'}</MathText>)

    expect(container.querySelector('p')).toBeNull()
    expect(container.querySelector('.katex')).not.toBeNull()
  })
})
