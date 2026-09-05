import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'

import NewSolutionPage from '@/app/classes/[id]/solutions/new/page'
import { api } from '@/lib/api'
import type { DocumentRead } from '@/types'

vi.mock('@/router/hooks', () => ({
  useParams: () => ({ id: '1' }),
  useRouter: () => ({ push: vi.fn() }),
}))

const documents: DocumentRead[] = Array.from({ length: 50 }, (_, index) => ({
  id: index + 1,
  class_id: 1,
  filename: `homework_${index + 1}.pdf`,
  mime: 'application/pdf',
  byte_size: 2048,
  state: 'ready',
  stage_detail: null,
  pages_total: 1,
  pages_done: 1,
  pages_skipped: 0,
  pages_failed: 0,
  recognize: false,
  error_message: null,
  created_at: '2026-09-04T12:00:00Z',
}))

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <NewSolutionPage />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.spyOn(api, 'getClass').mockResolvedValue({
    id: 1,
    name: 'Signals',
    code: null,
    semester: null,
    archived: false,
    document_count: 50,
    created_at: '2026-09-04',
    last_active_at: '2026-09-04',
  })
})

it('shows one recoverable error instead of an empty-library claim', async () => {
  vi.spyOn(api, 'listDocuments')
    .mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValueOnce(documents)
  renderPage()
  await screen.findByText('Could not load your documents')
  expect(screen.queryByText('Nothing to solve yet')).not.toBeInTheDocument()
  expect(screen.queryByText('No documents in this class yet.')).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect((await screen.findAllByRole('checkbox', { name: /homework_50.pdf/ }))[0]).toBeVisible()
})

it('keeps references optional and preserves their selection when collapsed', async () => {
  vi.spyOn(api, 'listDocuments').mockResolvedValue(documents)
  renderPage()
  await screen.findAllByRole('checkbox', { name: /homework_1.pdf/ })
  const disclosure = screen.getByText('Reference solutions (optional)').closest('details')!
  expect(disclosure).not.toHaveAttribute('open')
  await userEvent.click(screen.getAllByRole('checkbox', { name: /homework_1.pdf/ })[0])
  expect(screen.getByRole('button', { name: 'Find problems' })).toBeEnabled()
  await userEvent.click(within(disclosure).getByText('Reference solutions (optional)'))
  await userEvent.type(within(disclosure).getByRole('textbox'), 'homework_50')
  await userEvent.click(within(disclosure).getByRole('checkbox', { name: /homework_50.pdf/ }))
  await userEvent.click(within(disclosure).getByText('Reference solutions (optional) · 1 selected'))
  expect(disclosure).not.toHaveAttribute('open')
  await userEvent.click(within(disclosure).getByText('Reference solutions (optional) · 1 selected'))
  expect(within(disclosure).getByRole('checkbox', { name: /homework_50.pdf/ })).toBeChecked()
})
