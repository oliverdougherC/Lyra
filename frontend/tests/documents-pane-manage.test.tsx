import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DocumentsPane } from '@/components/documents/documents-pane'
import { api } from '@/lib/api'
import type { ClassRead, DocumentRead } from '@/types'

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
  useParams: () => ({ id: '1' }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => '/classes/1',
}))

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return { wrapper }
}

const DOCUMENTS = [
  {
    id: 3,
    class_id: 1,
    filename: 'homework_2.pdf',
    byte_size: 1024,
    state: 'ready',
    created_at: '2026-08-04 09:00:00',
  },
  {
    id: 5,
    class_id: 1,
    filename: 'syllabus.pdf',
    byte_size: 2048,
    state: 'ready',
    created_at: '2026-08-03 09:00:00',
  },
] as DocumentRead[]

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'listDocuments').mockResolvedValue(DOCUMENTS)
  vi.spyOn(api, 'getDocumentStatus').mockResolvedValue({ state: 'ready' } as never)
  vi.spyOn(api, 'listClasses').mockResolvedValue([
    { id: 1, name: 'Signals', archived: false },
    { id: 2, name: 'Linear Algebra', archived: false },
  ] as ClassRead[])
})

describe('DocumentsPane in the manage variant', () => {
  it('keeps the bulk bar out of the way until something is picked', async () => {
    const { wrapper } = createWrapper()

    render(<DocumentsPane classId={1} variant="manage" />, { wrapper })
    await screen.findByRole('button', { name: 'Select homework_2.pdf' })

    // Not merely hidden: controls for an action that cannot be taken should not be
    // reachable by keyboard either.
    expect(screen.queryByRole('button', { name: 'Move to class' })).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Select homework_2.pdf' }))

    expect(screen.getByText('1 file selected')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Move to class' })).toBeInTheDocument()
  })

  it('counts every file picked, and lets them all go again', async () => {
    const { wrapper } = createWrapper()

    render(<DocumentsPane classId={1} variant="manage" />, { wrapper })
    await userEvent.click(await screen.findByRole('button', { name: 'Select homework_2.pdf' }))
    await userEvent.click(screen.getByRole('button', { name: 'Select syllabus.pdf' }))

    expect(screen.getByText('2 files selected')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Clear' }))

    expect(screen.queryByText(/files selected/)).not.toBeInTheDocument()
  })

  it('confirms before deleting several files at once', async () => {
    const remove = vi.spyOn(api, 'deleteDocument').mockResolvedValue(undefined)
    const { wrapper } = createWrapper()

    render(<DocumentsPane classId={1} variant="manage" />, { wrapper })
    await userEvent.click(await screen.findByRole('button', { name: 'Select homework_2.pdf' }))
    await userEvent.click(screen.getByRole('button', { name: 'Select syllabus.pdf' }))
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByText('Delete 2 files?')).toBeInTheDocument()
    expect(remove).not.toHaveBeenCalled()

    await userEvent.click(within(dialog).getByRole('button', { name: 'Delete' }))

    await waitFor(() => expect(remove).toHaveBeenCalledTimes(2))
  })

  it('offers a move on each row, which the reading column does not', async () => {
    const { wrapper } = createWrapper()

    const view = render(<DocumentsPane classId={1} variant="manage" />, { wrapper })
    await userEvent.click(await screen.findByRole('button', { name: 'Actions for syllabus.pdf' }))
    expect(await screen.findByRole('menuitem', { name: /Move to another class/ })).toBeVisible()

    await userEvent.keyboard('{Escape}')
    view.rerender(
      <DocumentsPane classId={1} selectedDocumentId={null} onSelectDocument={() => {}} />,
    )

    await userEvent.click(await screen.findByRole('button', { name: 'Actions for syllabus.pdf' }))
    expect(
      screen.queryByRole('menuitem', { name: /Move to another class/ }),
    ).not.toBeInTheDocument()
  })
})
