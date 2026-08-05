import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { MoveDocumentDialog } from '@/components/documents/move-document-dialog'
import { api } from '@/lib/api'
import { documentKeys } from '@/lib/hooks/use-documents'
import type { ClassRead, DocumentRead } from '@/types'

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return { queryClient, wrapper }
}

const CLASSES = [
  { id: 1, name: 'Signals', code: 'ECE 203', archived: false },
  { id: 2, name: 'Linear Algebra', code: 'MATH 250', archived: false },
  { id: 3, name: 'Last term', code: 'PHYS 101', archived: true },
] as ClassRead[]

function document(id: number, filename: string): DocumentRead {
  return { id, class_id: 1, filename, state: 'ready' } as DocumentRead
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'listClasses').mockResolvedValue(CLASSES)
})

describe('MoveDocumentDialog', () => {
  it('offers every other class, including archived ones', async () => {
    const { wrapper } = createWrapper()

    render(
      <MoveDocumentDialog
        documents={[document(9, 'lecture-2.pdf')]}
        classId={1}
        onOpenChange={() => {}}
      />,
      { wrapper },
    )

    expect(await screen.findByText('Linear Algebra')).toBeInTheDocument()
    // A class put away at the end of a term is exactly where last term's notes belong.
    expect(screen.getByText('Last term')).toBeInTheDocument()
    // The class the file is already in is not somewhere to move it.
    expect(screen.queryByText('Signals')).not.toBeInTheDocument()
  })

  it('moves every selected file to the chosen class', async () => {
    const move = vi
      .spyOn(api, 'moveDocument')
      .mockImplementation(async (documentId) => document(documentId, 'moved.pdf'))
    const onOpenChange = vi.fn()
    const { wrapper } = createWrapper()

    render(
      <MoveDocumentDialog
        documents={[document(9, 'a.pdf'), document(10, 'b.pdf')]}
        classId={1}
        onOpenChange={onOpenChange}
      />,
      { wrapper },
    )

    await userEvent.click(await screen.findByText('Linear Algebra'))
    await userEvent.click(screen.getByRole('button', { name: 'Move' }))

    await waitFor(() => expect(move).toHaveBeenCalledTimes(2))
    expect(move).toHaveBeenCalledWith(9, 2)
    expect(move).toHaveBeenCalledWith(10, 2)
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('cannot be confirmed until a destination is chosen', async () => {
    const { wrapper } = createWrapper()

    render(
      <MoveDocumentDialog documents={[document(9, 'a.pdf')]} classId={1} onOpenChange={() => {}} />,
      { wrapper },
    )

    expect(await screen.findByRole('button', { name: 'Move' })).toBeDisabled()
  })

  it('refreshes both class document lists, since the file leaves one and joins the other', async () => {
    vi.spyOn(api, 'moveDocument').mockResolvedValue(document(9, 'a.pdf'))
    const { queryClient, wrapper } = createWrapper()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    render(
      <MoveDocumentDialog documents={[document(9, 'a.pdf')]} classId={1} onOpenChange={() => {}} />,
      { wrapper },
    )

    await userEvent.click(await screen.findByText('Linear Algebra'))
    await userEvent.click(screen.getByRole('button', { name: 'Move' }))

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: documentKeys.list(2) }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: documentKeys.list(1) })
  })

  it('says so when there is nowhere to move the file to', async () => {
    vi.spyOn(api, 'listClasses').mockResolvedValue([CLASSES[0]])
    const { wrapper } = createWrapper()

    render(
      <MoveDocumentDialog documents={[document(9, 'a.pdf')]} classId={1} onOpenChange={() => {}} />,
      { wrapper },
    )

    expect(await screen.findByText(/nowhere to move this to yet/i)).toBeInTheDocument()
  })
})
