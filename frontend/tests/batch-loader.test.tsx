import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { BatchLoader } from '@/components/documents/batch-loader'

/**
 * The finished batch's icon is the load-bearing signal PLA-293 is about: a batch with any
 * unusable item must never wear the success check. `needsAttention` drives that, folding
 * together failures and unsupported input, so a positive value always shows the alert
 * state regardless of how many items succeeded alongside it.
 */
describe('BatchLoader summary icon', () => {
  it('shows the success state when nothing needs attention', () => {
    render(
      <BatchLoader
        title="All documents processed"
        processed={3}
        needsAttention={0}
        total={3}
        complete
      />,
    )

    const status = screen.getByRole('status')
    expect(status.querySelector('.bg-success-fill')).not.toBeNull()
    expect(status.querySelector('.bg-danger-fill')).toBeNull()
  })

  it('shows the attention state when an item is unusable, even beside successes', () => {
    render(
      <BatchLoader
        title="1 item needs attention"
        processed={2}
        needsAttention={1}
        total={3}
        complete
      />,
    )

    const status = screen.getByRole('status')
    expect(status.querySelector('.bg-danger-fill')).not.toBeNull()
    expect(status.querySelector('.bg-success-fill')).toBeNull()
  })

  it('counts every settled item, usable or not, toward the readout', () => {
    render(
      <BatchLoader
        title="2 items need attention"
        processed={1}
        needsAttention={2}
        total={3}
        complete
      />,
    )

    // processed + needsAttention items have all settled: the count reads the full batch.
    expect(screen.getByText('3 of 3')).toBeInTheDocument()
  })
})
