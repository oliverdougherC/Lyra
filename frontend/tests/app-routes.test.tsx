import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

function resetLocation(url: string) {
  window.history.replaceState({}, '', url)
}

describe('AppRoutes lazy failures', () => {
  beforeEach(() => {
    resetLocation('/#/settings')
  })

  afterEach(() => {
    vi.resetModules()
    vi.clearAllMocks()
    vi.doUnmock('@/app/settings/page')
  })

  it('shows the route fallback and reloads the app when a lazy route import fails', async () => {
    const reload = vi.fn()
    const original = window.location
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...original, reload },
    })

    vi.doMock('@/app/settings/page', () => {
      throw new Error('settings chunk missing')
    })

    const { RouterProvider } = await import('@/router/hooks')
    const { AppRoutes } = await import('@/router/app-routes')

    render(
      <RouterProvider>
        <AppRoutes />
      </RouterProvider>,
    )

    await screen.findByRole('alert')
    expect(screen.getByText(/something went wrong/i)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(reload).toHaveBeenCalledTimes(1)

    Object.defineProperty(window, 'location', { configurable: true, value: original })
  })
})
