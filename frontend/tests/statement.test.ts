import { describe, expect, it } from 'vitest'

import { statementLeadIn } from '@/lib/statement'

/**
 * The segmenter copies statements verbatim, so a problem with five lettered parts comes
 * back with all five inside the statement and again as structured parts. Both were raw
 * before, so the repetition at least matched; now the parts are typeset and the statement
 * is not, and printing both shows the same problem twice in two notations.
 */
describe('statementLeadIn', () => {
  const Q1 = [
    'Q1.',
    'Compute X(jω) of the following signals x(t):',
    '(a)',
    'x(t) = e−2tu(t −3)',
    '(b)',
    'x(t) = e−4|t|',
  ].join('\n')

  it('keeps the text that introduces the sub-parts and drops the list', () => {
    expect(statementLeadIn(Q1, ['(a)', '(b)'])).toBe(
      'Q1.\nCompute X(jω) of the following signals x(t):',
    )
  })

  it('leaves a problem with no sub-parts alone', () => {
    expect(statementLeadIn('Find the transform of x(t).', [])).toBe('Find the transform of x(t).')
  })

  it('leaves the statement alone when the segmenter did not repeat the parts', () => {
    // The prompt asks for exactly this. When it lands, there is nothing to cut.
    const lead = 'Compute X(jω) of the following signals x(t):'

    expect(statementLeadIn(lead, ['(a)', '(b)'])).toBe(lead)
  })

  it('does not cut when the label is the very first line', () => {
    // Everything would be dropped, leaving the problem with no statement at all.
    expect(statementLeadIn('(a) Sketch it.\n(b) Explain.', ['(a)', '(b)'])).toBe(
      '(a) Sketch it.\n(b) Explain.',
    )
  })

  it('leaves unlabelled parts alone rather than guessing', () => {
    expect(statementLeadIn(Q1, [null, ''])).toBe(Q1)
  })
})
