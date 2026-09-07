import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'

import SolutionPage from '@/app/classes/[id]/solutions/[artifactId]/page'
import type { SolutionDetail } from '@/types'

const state = vi.hoisted(() => ({
  data: undefined as SolutionDetail | undefined,
  refetch: vi.fn(),
  isFetching: false,
}))
vi.mock('@/router/hooks', () => ({
  useParams: () => ({ id: '6', artifactId: '1' }),
  useRouter: () => ({ push: vi.fn() }),
}))
vi.mock('@/components/layout/page-chrome', () => ({
  useFullBleed: vi.fn(),
  useImmersiveChrome: vi.fn(),
  HeaderCrumb: () => null,
  HeaderActions: () => null,
}))
vi.mock('@/lib/hooks/use-classes', () => ({ useClasses: () => ({ data: [] }) }))
vi.mock('@/lib/hooks/use-solutions', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/hooks/use-solutions')>()),
  useSolution: () => ({ ...state, isError: true, isPending: false, error: new Error('offline') }),
  useSolutionStatus: () => ({ data: undefined }),
}))
function renderPage() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <SolutionPage />
    </QueryClientProvider>,
  )
}
it('retains cached questions and offers retry after a refresh fails', async () => {
  state.data = {
    id: 1,
    class_id: 6,
    kind: 'solution_set',
    title: 'Saved questions',
    state: 'awaiting_review',
    stage_detail: null,
    error_message: null,
    problems_total: 1,
    problems_done: 0,
    created_at: '',
    updated_at: '',
    sources: [],
    parts: [
      {
        id: 10,
        artifact_id: 1,
        parent_part_id: null,
        ordinal: 0,
        label: 'Problem 1',
        content: 'Find the transform.',
        content_type: 'markdown',
        kind: 'problem',
        status: 'pending',
        origin: 'generated',
        verdict: 'unchecked',
        verdict_detail: null,
        solve_parts: 'together',
        error_message: null,
        provenance: [],
        checks: [],
      },
    ],
  }
  renderPage()
  expect(screen.getByText('Find the transform.')).toBeVisible()
  expect(screen.getByText(/Showing the last loaded version/)).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(state.refetch).toHaveBeenCalled()
})
it('shows retry progress when the initial load failed', () => {
  state.data = undefined
  state.isFetching = true
  renderPage()
  expect(screen.getByRole('button', { name: 'Retrying…' })).toBeDisabled()
})
