import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import ClassPage from '@/app/classes/[id]/page'
const state = vi.hoisted(() => ({ refetch: vi.fn(), isFetching: false }))
vi.mock('@/router/hooks', () => ({
  useParams: () => ({ id: '7' }),
  useRouter: () => ({ replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams('tab=files'),
}))
vi.mock('@/lib/hooks/use-classes', () => ({
  useClass: () => ({ ...state, data: { id: 7 }, isError: true }),
}))
vi.mock('@/components/classes/class-hub', () => ({
  HUB_TABS: ['ask', 'practice', 'work', 'files'],
  LEGACY_HUB_TABS: {},
  LEGACY_HUB_WORK_FILTERS: {},
  readHubTab: (tab: string) => tab,
  ClassHub: ({ tab }: { tab: string }) => <div>Saved {tab}</div>,
}))
it('keeps the current class destination visible when its metadata refresh fails', async () => {
  const view = render(<ClassPage />)
  expect(screen.getByText('Saved files')).toBeVisible()
  expect(screen.getByText('Could not refresh this class')).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(state.refetch).toHaveBeenCalledOnce()
  state.isFetching = true
  view.rerender(<ClassPage />)
  expect(screen.getByRole('button', { name: 'Retrying…' })).toBeDisabled()
  expect(screen.getByText('Saved files')).toBeVisible()
})
