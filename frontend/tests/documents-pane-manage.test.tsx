import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DocumentsPane } from '@/components/documents/documents-pane'
import { api } from '@/lib/api'
import type { ClassRead, DocumentRead } from '@/types'

vi.mock('@/router/hooks', () => ({
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
  sessionStorage.clear()
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

it('confirms single deletion in the chat pane and preserves selection when cancelled', async () => {
  const remove = vi.spyOn(api, 'deleteDocument').mockResolvedValue(undefined)
  const select = vi.fn()
  render(<DocumentsPane classId={1} selectedDocumentId={5} onSelectDocument={select} />, {
    wrapper: createWrapper().wrapper,
  })
  await userEvent.click(await screen.findByRole('button', { name: 'Actions for syllabus.pdf' }))
  await userEvent.click(await screen.findByRole('menuitem', { name: 'Delete' }))
  const dialog = await screen.findByRole('alertdialog')
  expect(within(dialog).getByText(/syllabus.pdf/)).toBeInTheDocument()
  expect(remove).not.toHaveBeenCalled()
  await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
  expect(select).not.toHaveBeenCalled()
})

it('keeps hidden selected files explicit and offers recovery from an unmatched search', async () => {
  vi.mocked(api.listDocuments).mockResolvedValue([
    ...DOCUMENTS,
    ...Array.from({ length: 7 }, (_, i) => ({
      ...DOCUMENTS[0],
      id: i + 10,
      filename: `notes-${i}.pdf`,
    })),
  ])
  render(<DocumentsPane classId={1} variant="manage" />, { wrapper: createWrapper().wrapper })
  await userEvent.click(await screen.findByRole('button', { name: 'Select syllabus.pdf' }))
  await userEvent.type(screen.getByRole('searchbox'), 'unmatched')
  expect(screen.getByText(/No documents match/)).toBeInTheDocument()
  expect(screen.queryByText('No documents yet')).not.toBeInTheDocument()
  expect(screen.getByText(/1 file selected/)).toBeInTheDocument()
  expect(screen.getByText(/1 hidden by filter/)).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
  expect(
    within(await screen.findByRole('alertdialog')).getByText('syllabus.pdf'),
  ).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  await userEvent.click(screen.getByRole('button', { name: 'Clear filter' }))
  expect(screen.getByRole('button', { name: 'Deselect syllabus.pdf' })).toBeInTheDocument()
})

it('retains failed deletes in the confirmation and selection while retiring successful files', async () => {
  vi.spyOn(api, 'deleteDocument').mockImplementation(async (id) => {
    if (id === 5) throw new Error('offline')
  })
  render(<DocumentsPane classId={1} variant="manage" />, { wrapper: createWrapper().wrapper })
  await userEvent.click(await screen.findByRole('button', { name: 'Select homework_2.pdf' }))
  await userEvent.click(screen.getByRole('button', { name: 'Select syllabus.pdf' }))
  await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
  await userEvent.click(
    within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete' }),
  )
  expect(await screen.findByRole('alert')).toHaveTextContent(
    '1 file could not be deleted. Try again.',
  )
  expect(
    within(screen.getByRole('alertdialog')).queryByText('homework_2.pdf'),
  ).not.toBeInTheDocument()
  expect(within(screen.getByRole('alertdialog')).getByText('syllabus.pdf')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(screen.getByText('1 file selected')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Deselect syllabus.pdf' })).toBeInTheDocument()
})

it('restores each class filter without copying the previous class query', async () => {
  sessionStorage.setItem('lyra:class:1:files-query', 'homework')
  sessionStorage.setItem('lyra:class:2:files-query', 'syllabus')
  const { wrapper } = createWrapper()
  const view = render(<DocumentsPane classId={1} variant="manage" />, { wrapper })
  expect(await screen.findByRole('searchbox', { name: 'Filter documents by name' })).toHaveValue(
    'homework',
  )
  view.rerender(<DocumentsPane classId={2} variant="manage" />)
  await waitFor(() =>
    expect(screen.getByRole('searchbox', { name: 'Filter documents by name' })).toHaveValue(
      'syllabus',
    ),
  )
  expect(sessionStorage.getItem('lyra:class:1:files-query')).toBe('homework')
  expect(sessionStorage.getItem('lyra:class:2:files-query')).toBe('syllabus')
})
