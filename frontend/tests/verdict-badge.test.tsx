import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { VerdictBadge, VERDICTS } from '@/components/solutions/verdict-badge'
import type { Verdict } from '@/types'

/**
 * The phase's central honesty rule, in the one place a reader actually meets it: nothing
 * that is not a check may look like a pass. Colour is never the only signal either, so the
 * labels have to be distinct from each other as text.
 */
describe('VerdictBadge', () => {
  it('gives every verdict its own label', () => {
    const labels = Object.values(VERDICTS).map((one) => one.label)

    expect(new Set(labels).size).toBe(labels.length)
  })

  it.each<[Verdict, string]>([
    ['verified', 'Checked'],
    ['refuted', 'Check failed'],
    ['uncheckable', 'Nothing to check'],
    ['unchecked', 'Not checked'],
  ])('renders %s as %s', (verdict, label) => {
    render(<VerdictBadge verdict={verdict} />)

    expect(screen.getByText(label)).toBeInTheDocument()
  })

  it('never renders a non-check as a check', () => {
    // `Not checked` is a complete sentence. Softening it to `Looks right`, or dropping the
    // badge entirely, would leave the student believing a check happened. `Nothing to
    // check` reads as fine — nothing went wrong — but it still may not claim the word.
    for (const verdict of ['unchecked', 'uncheckable'] as const) {
      const { unmount } = render(<VerdictBadge verdict={verdict} />)
      expect(screen.queryByText(VERDICTS.verified.label)).not.toBeInTheDocument()
      unmount()
    }
  })

  it('says no calculation was run before saying anything went well', () => {
    // The sentence that reconciles a green badge reading `Nothing to check` with a checker
    // that wrote a paragraph about why every step is correct. Without it the two contradict
    // each other on screen and the student has no way to tell which to believe.
    expect(VERDICTS.uncheckable.explanation).toMatch(/^Nothing here could be settled/)
  })

  it('keeps a job left undone looking undone', () => {
    // `unchecked` is the one state where something was owed and did not arrive, so it is
    // the one state that must not read as fine.
    expect(VERDICTS.unchecked.className).not.toContain('success')
    expect(VERDICTS.uncheckable.className).toContain('success')
  })

  it('prefers the backend reason over the generic one', () => {
    // The generic explanation is a fallback. When the backend named the check that
    // disagreed, that sentence is the more useful of the two.
    render(<VerdictBadge verdict="refuted" detail="The integral in step 3 returns 4/3." />)

    expect(screen.getByText('Check failed')).toBeInTheDocument()
  })
})
