import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DocumentNicknameDialog } from '@/components/documents/document-nickname-dialog'
import { ApiError, api } from '@/lib/api'
import type { DocumentRead } from '@/types'

const original = {
  id: 7,
  class_id: 1,
  filename: 'opaque.pdf',
  nickname: 'A',
  state: 'ready',
  created_at: '2026-09-01 00:00:00',
} as DocumentRead

const queryClient = new QueryClient()
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}

beforeEach(() => {
  vi.restoreAllMocks()
  queryClient.clear()
})

describe('nickname edit session', () => {
  it('keeps its original CAS baseline through a refetch and requires an explicit conflict choice', async () => {
    const user = userEvent.setup()
    const onOpenChange = vi.fn()
    const current = { ...original, nickname: 'C' }
    const save = vi
      .spyOn(api, 'updateDocumentNickname')
      .mockRejectedValueOnce(new ApiError(409, 'Nickname changed.'))
      .mockResolvedValueOnce({ ...original, nickname: 'B' })
    vi.spyOn(api, 'getDocument').mockResolvedValue(current)
    const view = render(
      <DocumentNicknameDialog document={original} open onOpenChange={onOpenChange} />,
      { wrapper },
    )
    const input = screen.getByRole('textbox', { name: 'Document nickname' })
    await user.clear(input)
    await user.type(input, 'B')
    view.rerender(<DocumentNicknameDialog document={current} open onOpenChange={onOpenChange} />)
    expect(input).toHaveValue('B')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(save).toHaveBeenCalledWith(7, 'B', 'A'))
    expect(await screen.findByText(/Current name: C/)).toBeInTheDocument()
    expect(input).toHaveValue('B')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(onOpenChange).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Keep my draft' }))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(save).toHaveBeenLastCalledWith(7, 'B', 'C'))
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
  })

  it('keeps reset on the session baseline and can adopt the current name after a conflict', async () => {
    const user = userEvent.setup()
    const current = { ...original, nickname: 'C' }
    const save = vi
      .spyOn(api, 'updateDocumentNickname')
      .mockRejectedValueOnce(new ApiError(409, 'Nickname changed.'))
      .mockResolvedValueOnce({ ...original, nickname: null })
    vi.spyOn(api, 'getDocument').mockResolvedValue(current)
    render(<DocumentNicknameDialog document={original} open onOpenChange={vi.fn()} />, { wrapper })
    await user.click(screen.getByRole('button', { name: 'Use original name' }))
    expect(await screen.findByText(/Current name: C/)).toBeInTheDocument()
    expect(save).toHaveBeenCalledWith(7, null, 'A')
    expect(screen.getByRole('button', { name: 'Use original name' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Use current name' }))
    expect(screen.getByRole('textbox', { name: 'Document nickname' })).toHaveValue('C')
    await user.click(screen.getByRole('button', { name: 'Use original name' }))
    await waitFor(() => expect(save).toHaveBeenLastCalledWith(7, null, 'C'))
  })

  it('resets after cancellation and when the editing document changes', async () => {
    const user = userEvent.setup()
    const onOpenChange = vi.fn()
    const view = render(
      <DocumentNicknameDialog document={original} open onOpenChange={onOpenChange} />,
      { wrapper },
    )
    const input = screen.getByRole('textbox', { name: 'Document nickname' })
    await user.clear(input)
    await user.type(input, 'unsaved')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onOpenChange).toHaveBeenCalledWith(false)
    view.rerender(
      <DocumentNicknameDialog document={original} open={false} onOpenChange={onOpenChange} />,
    )
    view.rerender(<DocumentNicknameDialog document={original} open onOpenChange={onOpenChange} />)
    expect(screen.getByRole('textbox', { name: 'Document nickname' })).toHaveValue('A')
    view.rerender(
      <DocumentNicknameDialog
        document={{ ...original, id: 8, filename: 'other.pdf', nickname: 'Other' }}
        open
        onOpenChange={onOpenChange}
      />,
    )
    expect(screen.getByRole('textbox', { name: 'Document nickname' })).toHaveValue('Other')
    const save = vi.spyOn(api, 'updateDocumentNickname').mockResolvedValue({ ...original, id: 8 })
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(save).toHaveBeenCalledWith(8, 'Other', 'Other'))
  })
})
