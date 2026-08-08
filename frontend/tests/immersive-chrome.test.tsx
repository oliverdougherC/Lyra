import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { AppHeader } from '@/components/layout/app-header'
import { ImmersiveProvider, useImmersiveChrome } from '@/components/layout/page-chrome'
import { SidebarProvider } from '@/components/ui/sidebar'

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
  useParams: () => ({}),
  useSearchParams: () => new URLSearchParams(),
  // Settings rather than a class route: the header's class-scoped half is not what these
  // are about, and rendering it would drag the profile sheet in with it.
  usePathname: () => '/settings',
}))

function createWrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // The header carries the sidebar trigger, which reads the sidebar's context.
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <SidebarProvider>{children}</SidebarProvider>
    </QueryClientProvider>
  )
  return { wrapper }
}

function Asker({ enabled }: { enabled: boolean }) {
  useImmersiveChrome(enabled)
  return null
}

describe('useImmersiveChrome', () => {
  it('hands the chrome back when the route that hid it goes away', () => {
    // The safety property the whole mode rests on. A student who navigates out of a draft
    // must not arrive somewhere with no sidebar and no header and no way to ask for one.
    const seen: boolean[] = []
    const view = render(
      <ImmersiveProvider onChange={(next) => seen.push(next)}>
        <Asker enabled />
      </ImmersiveProvider>,
    )

    expect(seen.at(-1)).toBe(true)

    view.unmount()

    expect(seen.at(-1)).toBe(false)
  })

  it('follows the route from hidden back to shown without unmounting', () => {
    const seen: boolean[] = []
    const view = render(
      <ImmersiveProvider onChange={(next) => seen.push(next)}>
        <Asker enabled />
      </ImmersiveProvider>,
    )
    view.rerender(
      <ImmersiveProvider onChange={(next) => seen.push(next)}>
        <Asker enabled={false} />
      </ImmersiveProvider>,
    )

    expect(seen.at(-1)).toBe(false)
  })
})

describe('AppHeader', () => {
  it('takes its row back and stops holding focus when collapsed', () => {
    const { wrapper } = createWrapper()

    render(<AppHeader collapsed />, { wrapper })

    const header = screen.getByRole('banner', { hidden: true })
    expect(header).toHaveAttribute('data-collapsed')
    // Height alone would leave the breadcrumb tabbable: a keyboard would land on a link
    // nobody can see, in a bar that is not on screen.
    expect(header).toHaveAttribute('inert')
    expect(header.className).toContain('h-0')
  })

  it('is an ordinary header otherwise', () => {
    const { wrapper } = createWrapper()

    render(<AppHeader />, { wrapper })

    const header = screen.getByRole('banner')
    expect(header).not.toHaveAttribute('data-collapsed')
    expect(header).not.toHaveAttribute('inert')
    expect(header.className).toContain('h-14')
  })
})
