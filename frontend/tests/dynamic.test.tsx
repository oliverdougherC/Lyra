import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import dynamic from '@/router/dynamic'

describe('dynamic()', () => {
  it('shows an explicit error state and reload-based retry when the chunk load fails', async () => {
    const reload = vi.fn()
    const original = window.location
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...original, reload },
    })

    const Broken = dynamic(
      async () => {
        throw new Error('chunk missing')
      },
      {
        loading: () => <p>Loading…</p>,
        error: (_error, retry) => (
          <div role="alert">
            <p>The editor chunk failed.</p>
            <button type="button" onClick={retry}>
              Retry
            </button>
          </div>
        ),
      },
    )

    const user = userEvent.setup()
    render(<Broken />)

    await screen.findByRole('alert')
    await user.click(screen.getByRole('button', { name: 'Retry' }))

    expect(reload).toHaveBeenCalledTimes(1)

    Object.defineProperty(window, 'location', { configurable: true, value: original })
  })
})
