import { render, screen } from '@testing-library/react'
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
 * document, including the head that `global-error` owns outright.
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

describe('app/global-error.tsx, the document boundary', () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.classList.remove('dark')
  })
  it('provides its own document: <html>, <head>, and <body>', () => {
    render(<GlobalErrorBoundary error={sensitiveError()} retry={() => {}} />)
    // React 19 resolves a client component's hoisted <html>/<head>/<body> against
    // the real document, which is exactly what a browser sees for this component:
    // the boundary owns the whole document.
    expect(document.documentElement.tagName).toBe('HTML')
    expect(document.documentElement.getAttribute('lang')).toBe('en')
    // The document must stand alone: a title and its own styles, and the
    // recovery UI inside the body it declares.
    expect(document.head.querySelector('title')?.textContent).toBe('Lyra')
    expect(document.head.querySelector('style')).toBeTruthy()
    const alert = document.body.querySelector('[role="alert"]')
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
