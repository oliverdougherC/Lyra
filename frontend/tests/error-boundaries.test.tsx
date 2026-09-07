import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import RouteErrorBoundary from '@/app/error'
import GlobalErrorBoundary from '@/app/global-error'
import { THEME_STORAGE_KEY } from '@/lib/theme'

/**
 * The error both boundaries receive. It carries every class of content the
 * fallbacks are contractually forbidden from printing: an exception message
 * with a query and a path, a student's draft text, and the server-side digest.
 */
function sensitiveError(): Error & { digest?: string } {
  return Object.assign(
    new Error(
      'DB: SELECT body FROM drafts WHERE id = 7 (body: "The student\'s draft: I think the mitochondria...") -- /var/lyra/data.db:12',
    ),
  )
}

const SECRET_FRAGMENTS = [
  'SELECT body',
  "The student's draft",
  'SECRET-DIGEST-9f8e7d',
  'data.db',
  'var/lyra',
]

/**
 * The boundaries may print nothing from the exception. The scan covers the whole
 * document, including the inline fallback styles.
 */
function assertNoSecrets() {
  const text = document.documentElement.textContent ?? ''
  for (const fragment of SECRET_FRAGMENTS) {
    expect(text).not.toContain(fragment)
  }
}

describe('app/error.tsx, the route-segment boundary', () => {
  it('renders a generic fallback instead of the exception', () => {
    render(<RouteErrorBoundary error={sensitiveError()} retry={() => {}} />)
    assertNoSecrets()
    expect(screen.getByRole('alert')).toBeTruthy()
    expect(screen.getByRole('heading', { level: 2, name: /something went wrong/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })

  it('recovers when the student clicks retry', async () => {
    const user = userEvent.setup()
    const retry = vi.fn()
    render(<RouteErrorBoundary error={sensitiveError()} retry={retry} />)
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(retry).toHaveBeenCalledTimes(1)
  })

  it('recovers from the keyboard', async () => {
    const user = userEvent.setup()
    const retry = vi.fn()
    render(<RouteErrorBoundary error={sensitiveError()} retry={retry} />)
    screen.getByRole('button', { name: 'Try again' }).focus()
    await user.keyboard('{Enter}')
    expect(retry).toHaveBeenCalledTimes(1)
  })
})

describe('app/global-error.tsx, the bootstrap boundary', () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.classList.remove('dark')
  })
  it('provides standalone styles and recovery UI without replacing the Vite document', () => {
    const { container } = render(<GlobalErrorBoundary error={sensitiveError()} retry={() => {}} />)
    expect(document.documentElement.tagName).toBe('HTML')
    expect(container.querySelector('style')).toBeTruthy()
    const alert = container.querySelector('[role="alert"]')
    expect(alert).toBeTruthy()
    expect(alert!.querySelector('button')).toBeTruthy()
  })

  it('applies the stored dark theme on mount, through the app theme key', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'dark')
    render(<GlobalErrorBoundary error={sensitiveError()} retry={() => {}} />)
    expect(document.documentElement.classList.contains('dark')).toBe(true)
  })

  it('keeps light when the stored choice is light', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'light')
    render(<GlobalErrorBoundary error={sensitiveError()} retry={() => {}} />)
    expect(document.documentElement.classList.contains('dark')).toBe(false)
  })

  it('follows the system setting when the stored choice is system', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'system')
    vi.stubGlobal(
      'matchMedia',
      vi.fn(() => ({ matches: true, media: '(prefers-color-scheme: dark)' })),
    )
    render(<GlobalErrorBoundary error={sensitiveError()} retry={() => {}} />)
    expect(document.documentElement.classList.contains('dark')).toBe(true)
  })

  it('renders a generic fallback instead of the exception', () => {
    render(<GlobalErrorBoundary error={sensitiveError()} retry={() => {}} />)
    assertNoSecrets()
    expect(screen.getByRole('alert')).toBeTruthy()
    expect(screen.getByRole('heading', { name: /something went wrong/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })

  it('recovers when the student clicks retry', async () => {
    const user = userEvent.setup()
    const retry = vi.fn()
    render(<GlobalErrorBoundary error={sensitiveError()} retry={retry} />)
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(retry).toHaveBeenCalledTimes(1)
  })
})

it.each(['false', 'exception'])(
  'shows a retry failure after %s without leaking details',
  async (outcome) => {
    const retry = vi.fn(async () => {
      if (outcome === 'exception') throw sensitiveError()
      return false
    })
    render(<GlobalErrorBoundary error={sensitiveError()} retry={retry} />)
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Lyra still could not start')
    expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled()
    assertNoSecrets()
  },
)
it('guards repeated startup recovery and announces successful recovery', async () => {
  let finish!: (value: boolean) => void
  const retry = vi.fn(
    () =>
      new Promise<boolean>((resolve) => {
        finish = resolve
      }),
  )
  render(<GlobalErrorBoundary error={sensitiveError()} retry={retry} />)
  await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
  const pending = screen.getByRole('button', { name: 'Trying again…' })
  expect(pending).toBeDisabled()
  await userEvent.click(pending)
  expect(retry).toHaveBeenCalledTimes(1)
  await act(async () => finish(true))
  expect(screen.getByRole('status')).toHaveTextContent('Recovered. Opening Lyra…')
})
