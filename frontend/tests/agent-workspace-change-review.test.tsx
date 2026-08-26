import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import {
  WorkspaceChangeReviewRail,
  hunksAreStale,
  type WorkspaceChangeReview,
  type HunkRef,
} from '@/components/agent'

const CHANGE: WorkspaceChangeReview = {
  id: 9,
  path: 'src/lab.py',
  rationale: 'Tighten the helper and preserve the existing call site.',
  state: 'partially_applied',
  summary: 'One hunk already landed; one still needs review.',
  hunks: [
    {
      hash: 'h1',
      index: 0,
      decision: 'accepted',
      lines: [' def helper(x):', '-    return x + 1', '+    return x + 2'],
    },
    {
      hash: 'h2',
      index: 1,
      header: '@@ -8,2 +8,2 @@',
      lines: [' print(helper(3))', '-print("old")', '+print("new")'],
    },
  ],
}

describe('WorkspaceChangeReviewRail', () => {
  it('renders settled and pending hunks distinctly and emits per-hunk actions', async () => {
    const onAcceptHunk = vi.fn()
    const onRejectHunk = vi.fn()
    render(
      <WorkspaceChangeReviewRail
        change={CHANGE}
        onAcceptHunk={onAcceptHunk}
        onRejectHunk={onRejectHunk}
      />,
    )

    expect(screen.getByText('Partially applied')).toBeInTheDocument()
    expect(screen.getByText(/1 hunk accepted/i)).toBeInTheDocument()
    expect(screen.getByText('Accepted')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Reject change 2' }))

    expect(onRejectHunk).toHaveBeenCalledWith(expect.objectContaining({ hash: 'h2', index: 1 }))
    expect(onAcceptHunk).not.toHaveBeenCalled()
  })

  it('swaps to side-by-side file review when the proposal is stale', () => {
    render(
      <WorkspaceChangeReviewRail
        change={{
          ...CHANGE,
          state: 'stale',
          currentContent: 'print(helper(3))',
          proposedContent: 'print(helper(4))',
        }}
      />,
    )

    expect(screen.getByText('Proposal is stale')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Accept change 2' })).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Current file' })).toHaveTextContent(
      'print(helper(3))',
    )
    expect(screen.getByRole('region', { name: 'Proposed file' })).toHaveTextContent(
      'print(helper(4))',
    )
  })
})

describe('hunksAreStale (PLA-303)', () => {
  const h = (index: number, hash: string): HunkRef => ({ index, hash })

  it('returns false when selected hunks match the fresh set exactly', () => {
    const selected = [h(0, 'aaa'), h(1, 'bbb')]
    const fresh = [h(0, 'aaa'), h(1, 'bbb')]
    expect(hunksAreStale(selected, fresh, 2)).toBe(false)
  })

  it('detects same-index changed content (hash differs)', () => {
    const selected = [h(0, 'aaa')]
    const fresh = [h(0, 'CHANGED')]
    expect(hunksAreStale(selected, fresh, 1)).toBe(true)
  })

  it('detects a hunk that disappeared (index no longer in fresh set)', () => {
    const selected = [h(0, 'aaa'), h(1, 'bbb')]
    const fresh = [h(0, 'aaa')]
    expect(hunksAreStale(selected, fresh, 2)).toBe(true)
  })

  it('detects insertion/reordering (fresh set has more hunks than displayed)', () => {
    const selected = [h(0, 'aaa')]
    const fresh = [h(0, 'aaa'), h(1, 'new')]
    expect(hunksAreStale(selected, fresh, 1)).toBe(true)
  })

  it('returns false for an unchanged partial accept (subset of hunks)', () => {
    const selected = [h(1, 'bbb')]
    const fresh = [h(0, 'aaa'), h(1, 'bbb')]
    expect(hunksAreStale(selected, fresh, 2)).toBe(false)
  })

  it('returns false for an unchanged retry of an already-accepted accept-all', () => {
    const selected = [h(0, 'aaa'), h(1, 'bbb')]
    const fresh = [h(0, 'aaa'), h(1, 'bbb')]
    expect(hunksAreStale(selected, fresh, 2)).toBe(false)
  })

  it('detects accept-all when displayed set no longer represents the refreshed set', () => {
    const selected = [h(0, 'aaa'), h(1, 'bbb')]
    const fresh = [h(0, 'aaa'), h(1, 'bbb'), h(2, 'ccc')]
    expect(hunksAreStale(selected, fresh, 2)).toBe(true)
  })

  it('detects deletion causing the fresh set to shrink', () => {
    const selected = [h(0, 'aaa')]
    const fresh = [h(0, 'aaa')]
    expect(hunksAreStale(selected, fresh, 3)).toBe(true)
  })
})
