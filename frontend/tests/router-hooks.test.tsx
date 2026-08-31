import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  ROUTE_ANCHOR_QUERY_KEY,
  RouterProvider,
  useNavigationVersion,
  useParams,
  usePathname,
  useRouteAnchor,
  useRouter,
  useSearchParams,
} from '@/router/hooks'

function resetLocation(url: string) {
  window.history.replaceState({}, '', url)
}

function RouterProbe() {
  const pathname = usePathname()
  const params = useParams<{ id?: string; artifactId?: string }>()
  const searchParams = useSearchParams()
  const routeAnchor = useRouteAnchor()
  const navigationVersion = useNavigationVersion()
  const router = useRouter()

  return (
    <div>
      <p data-testid="pathname">{pathname}</p>
      <p data-testid="class-id">{params.id ?? ''}</p>
      <p data-testid="artifact-id">{params.artifactId ?? ''}</p>
      <p data-testid="tab">{searchParams.get('tab') ?? ''}</p>
      <p data-testid="anchor">{routeAnchor ?? ''}</p>
      <p data-testid="nav-version">{String(navigationVersion)}</p>
      <button type="button" onClick={() => router.pushAnchor('source-2')}>
        Jump to source 2
      </button>
      <button type="button" onClick={() => router.replaceAnchor('source-2')}>
        Jump to source 2 again
      </button>
    </div>
  )
}

function isEventType(
  call: Parameters<typeof window.addEventListener> | Parameters<typeof window.removeEventListener>,
  type: 'hashchange' | 'popstate',
) {
  return call[0] === type
}

describe('router hooks', () => {
  beforeEach(() => {
    resetLocation('/#/')
  })

  it('keeps runtime-created ids and route anchor state separate', () => {
    resetLocation(
      `/#/classes/runtime-class/drafts/runtime-draft?tab=plan&${ROUTE_ANCHOR_QUERY_KEY}=source-9`,
    )

    render(
      <RouterProvider>
        <RouterProbe />
      </RouterProvider>,
    )

    expect(screen.getByTestId('pathname')).toHaveTextContent(
      '/classes/runtime-class/drafts/runtime-draft',
    )
    expect(screen.getByTestId('class-id')).toHaveTextContent('runtime-class')
    expect(screen.getByTestId('artifact-id')).toHaveTextContent('runtime-draft')
    expect(screen.getByTestId('tab')).toHaveTextContent('plan')
    expect(screen.getByTestId('anchor')).toHaveTextContent('source-9')
  })

  it('canonically keeps the current route when a legacy source fragment arrives', async () => {
    resetLocation('/#/classes/7/drafts/runtime-draft?tab=plan')

    render(
      <RouterProvider>
        <RouterProbe />
      </RouterProvider>,
    )

    await act(async () => {
      window.location.hash = '#source-12'
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })

    expect(screen.getByTestId('pathname')).toHaveTextContent('/classes/7/drafts/runtime-draft')
    expect(screen.getByTestId('tab')).toHaveTextContent('plan')
    expect(screen.getByTestId('anchor')).toHaveTextContent('source-12')
    expect(window.location.hash).toBe(
      '#/classes/7/drafts/runtime-draft?tab=plan&lyra-anchor=source-12',
    )
  })

  it('replays repeated anchor jumps without stacking duplicate route changes', async () => {
    const user = userEvent.setup()
    resetLocation('/#/classes/7/drafts/9?tab=plan')

    render(
      <RouterProvider>
        <RouterProbe />
      </RouterProvider>,
    )

    await user.click(screen.getByRole('button', { name: 'Jump to source 2' }))
    const firstVersion = Number(screen.getByTestId('nav-version').textContent)
    expect(window.location.hash).toBe('#/classes/7/drafts/9?tab=plan&lyra-anchor=source-2')

    await user.click(screen.getByRole('button', { name: 'Jump to source 2 again' }))

    expect(Number(screen.getByTestId('nav-version').textContent)).toBeGreaterThan(firstVersion)
    expect(window.location.hash).toBe('#/classes/7/drafts/9?tab=plan&lyra-anchor=source-2')
  })

  it('preserves query state through back, forward, and a route remount', async () => {
    const user = userEvent.setup()
    resetLocation('/#/classes/7/drafts/9?tab=plan')

    const view = render(
      <RouterProvider>
        <RouterProbe />
      </RouterProvider>,
    )

    await user.click(screen.getByRole('button', { name: 'Jump to source 2' }))
    expect(window.location.hash).toBe('#/classes/7/drafts/9?tab=plan&lyra-anchor=source-2')

    await act(async () => {
      window.history.back()
    })
    await waitFor(() => {
      expect(screen.getByTestId('tab')).toHaveTextContent('plan')
      expect(screen.getByTestId('anchor')).toHaveTextContent('')
    })

    await act(async () => {
      window.history.forward()
    })
    await waitFor(() => expect(screen.getByTestId('anchor')).toHaveTextContent('source-2'))

    view.unmount()
    render(
      <RouterProvider>
        <RouterProbe />
      </RouterProvider>,
    )

    expect(screen.getByTestId('tab')).toHaveTextContent('plan')
    expect(screen.getByTestId('anchor')).toHaveTextContent('source-2')
  })

  it('removes its hashchange and popstate listeners on unmount', () => {
    const addEventListener = vi.spyOn(window, 'addEventListener')
    const removeEventListener = vi.spyOn(window, 'removeEventListener')

    for (let iteration = 0; iteration < 3; iteration += 1) {
      const view = render(
        <RouterProvider>
          <RouterProbe />
        </RouterProvider>,
      )
      view.unmount()
    }

    expect(
      addEventListener.mock.calls.filter((call) => isEventType(call, 'hashchange')),
    ).toHaveLength(3)
    expect(
      addEventListener.mock.calls.filter((call) => isEventType(call, 'popstate')),
    ).toHaveLength(3)
    expect(
      removeEventListener.mock.calls.filter((call) => isEventType(call, 'hashchange')),
    ).toHaveLength(3)
    expect(
      removeEventListener.mock.calls.filter((call) => isEventType(call, 'popstate')),
    ).toHaveLength(3)
  })
})
