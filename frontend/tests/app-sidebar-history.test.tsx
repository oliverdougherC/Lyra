import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { AppSidebar } from '@/components/layout/app-sidebar'
import { TooltipProvider } from '@/components/ui/tooltip'
import { SidebarProvider } from '@/components/ui/sidebar'

const state = vi.hoisted(() => ({ path: '/classes/1/solutions/100', session: '100' }))
vi.mock('@/router/hooks', () => ({
  usePathname: () => state.path,
  useSearchParams: () => new URLSearchParams({ session: state.session }),
}))
vi.mock('@/lib/hooks/use-classes', () => ({
  useClasses: () => ({ data: [{ id: 1, name: 'Calculus', color: '#123456' }], isPending: false }),
  useUpdateClass: () => ({ mutate: vi.fn() }),
}))
vi.mock('@/lib/hooks/use-chat', () => ({
  useSessions: () => ({
    data: Array.from({ length: 100 }, (_, i) => ({
      id: i + 1,
      title: `Conversation ${i + 1}`,
      created_at: '2026-09-01',
    })),
  }),
}))
vi.mock('@/lib/hooks/use-solutions', () => ({
  useSolutions: () => ({
    data: Array.from({ length: 100 }, (_, i) => ({
      id: i + 1,
      title: `Solution ${i + 1}`,
      state: 'ready',
      updated_at: '2026-09-01',
    })),
  }),
}))
vi.mock('@/lib/hooks/use-drafts', () => ({ useDrafts: () => ({ data: [] }) }))
vi.mock('@/lib/hooks/use-study', () => ({
  useStudyList: () => ({ data: { decks: [], quizzes: [] } }),
}))

it('opens Work and keeps older selected work and conversations reachable', () => {
  render(
    <TooltipProvider>
      <SidebarProvider>
        <AppSidebar />
      </SidebarProvider>
    </TooltipProvider>,
  )
  expect(screen.getByRole('link', { name: 'Work' })).toHaveAttribute(
    'href',
    '/#/classes/1?tab=work',
  )
  expect(screen.getByRole('link', { name: 'Solution 100' })).toHaveAttribute('data-active', 'true')
  expect(screen.getByRole('link', { name: 'Conversation 100' })).toHaveAttribute(
    'data-active',
    'true',
  )
  expect(screen.getAllByRole('link', { name: /^Conversation/ })).toHaveLength(6)
})

it('searches and pages history without expanding a hundred rows', async () => {
  render(
    <TooltipProvider>
      <SidebarProvider>
        <AppSidebar />
      </SidebarProvider>
    </TooltipProvider>,
  )
  await userEvent.click(screen.getByRole('button', { name: 'Find a conversation' }))
  await userEvent.click(screen.getByRole('button', { name: 'Next' }))
  expect(screen.getByRole('link', { name: 'Conversation 6' })).toBeInTheDocument()
  expect(screen.getAllByRole('link', { name: /^Conversation/ })).toHaveLength(6)
  await userEvent.type(
    screen.getByRole('textbox', { name: 'Search conversations' }),
    'Conversation 99',
  )
  expect(screen.getByRole('link', { name: 'Conversation 99' })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Conversation 100' })).toBeInTheDocument()
  expect(screen.getAllByRole('link', { name: /^Conversation/ })).toHaveLength(2)
  expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled()
})
