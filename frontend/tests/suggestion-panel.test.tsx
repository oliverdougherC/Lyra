import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { toast } from 'sonner'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { SuggestionPanel } from '@/components/drafts/suggestion-panel'
import { api, ApiError, DraftBodyConflictError } from '@/lib/api'
import type { PendingEdit } from '@/types'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
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

function hunk(index: number, lines: string[]) {
  return {
    index,
    old_start: index * 4,
    old_lines: lines.filter((line) => line[0] !== '+').length,
    new_start: index * 4,
    new_lines: lines.filter((line) => line[0] !== '-').length,
    lines,
    hash: `hash-${index}`,
  }
}

const EDIT: PendingEdit = {
  id: 5,
  stale: false,
  note: 'Tighten the introduction',
  proposed_content: 'The revised document.',
  hunks: [
    hunk(0, [' The opening line.', '-A longwinded sentence.', '+A shorter one.']),
    hunk(1, [' Later on.', '-Second old line.', '+Second new line.']),
  ],
}

function renderPanel(edit: PendingEdit, onApplied = vi.fn()) {
  const { wrapper } = createWrapper()
  render(
    <SuggestionPanel
      draftId={3}
      edit={edit}
      currentBody="The current document."
      onApplied={onApplied}
    />,
    { wrapper },
  )
  return { onApplied }
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('SuggestionPanel', () => {
  it('titles itself with the instruction and renders each hunk as a small diff card', () => {
    renderPanel(EDIT)

    expect(screen.getByText('Tighten the introduction')).toBeInTheDocument()

    const removed = screen.getByText('A longwinded sentence.')
    expect(removed.closest('div')).toHaveClass('bg-danger-fill', 'text-danger-text')
    const added = screen.getByText('A shorter one.')
    expect(added.closest('div')).toHaveClass('bg-success-fill', 'text-success-text')
    // Context lines stay quiet: they are there to anchor the eye, not to be acted on.
    expect(screen.getByText('The opening line.').closest('div')).not.toHaveClass('bg-danger-fill')
  })

  it('accepts one hunk by echoing its index and hash', async () => {
    const result = { remaining: 1, edit: { ...EDIT, hunks: [EDIT.hunks[1]] } }
    const accept = vi.spyOn(api, 'acceptPendingEdit').mockResolvedValue(result)
    const { onApplied } = renderPanel(EDIT)

    await userEvent.click(screen.getByRole('button', { name: 'Accept change 1' }))

    await waitFor(() =>
      expect(accept).toHaveBeenCalledWith(5, {
        hunk: { index: 0, hash: 'hash-0' },
        force: undefined,
      }),
    )
    await waitFor(() => expect(onApplied).toHaveBeenCalledWith(result))
  })

  it('rejects one hunk without touching the document', async () => {
    const result = { remaining: 1, edit: { ...EDIT, hunks: [EDIT.hunks[0]] } }
    const reject = vi.spyOn(api, 'rejectPendingEdit').mockResolvedValue(result)
    const accept = vi.spyOn(api, 'acceptPendingEdit').mockResolvedValue({ remaining: 0 })
    renderPanel(EDIT)

    await userEvent.click(screen.getByRole('button', { name: 'Reject change 2' }))

    await waitFor(() => expect(reject).toHaveBeenCalledWith(5, { index: 1, hash: 'hash-1' }))
    expect(accept).not.toHaveBeenCalled()
  })

  it('accepts and rejects the whole suggestion from the header', async () => {
    const accept = vi.spyOn(api, 'acceptPendingEdit').mockResolvedValue({ remaining: 0 })
    const reject = vi.spyOn(api, 'rejectPendingEdit').mockResolvedValue({ remaining: 0 })
    const { onApplied } = renderPanel(EDIT)

    await userEvent.click(screen.getByRole('button', { name: 'Accept all' }))
    await waitFor(() => expect(accept).toHaveBeenCalledWith(5, { hunk: undefined, force: false }))
    await waitFor(() => expect(onApplied).toHaveBeenCalledWith({ remaining: 0 }))

    await userEvent.click(screen.getByRole('button', { name: 'Reject all' }))
    await waitFor(() => expect(reject).toHaveBeenCalledWith(5, undefined))
  })

  it('toasts the server message and refetches when a hunk has raced', async () => {
    vi.spyOn(api, 'acceptPendingEdit').mockRejectedValue(
      new ApiError(409, 'That hunk changed since it was fetched. Re-fetch the suggestion.'),
    )
    renderPanel(EDIT)

    await userEvent.click(screen.getByRole('button', { name: 'Accept change 1' }))

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'That hunk changed since it was fetched. Re-fetch the suggestion.',
      ),
    )
  })

  it('swaps to a side-by-side reading when the edit is stale', () => {
    const stale: PendingEdit = {
      ...EDIT,
      stale: true,
      base_content: 'The document as the proposal remembers it.',
    }
    renderPanel(stale)

    // No hunk cards: the pieces no longer anchor, so piecemeal review is gone.
    expect(screen.queryByRole('button', { name: /Accept change/ })).not.toBeInTheDocument()
    expect(screen.getByText(/no longer line up with the document/)).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Current document' })).toHaveTextContent(
      'The current document.',
    )
    expect(screen.getByRole('region', { name: 'Proposed document' })).toHaveTextContent(
      'The revised document.',
    )
  })

  it('replaces the document only through a forced accept, and rejects a stale edit outright', async () => {
    const accept = vi.spyOn(api, 'acceptPendingEdit').mockResolvedValue({ remaining: 0 })
    const reject = vi.spyOn(api, 'rejectPendingEdit').mockResolvedValue({ remaining: 0 })
    const stale: PendingEdit = { ...EDIT, stale: true, base_content: 'The old base.' }
    renderPanel(stale)

    await userEvent.click(screen.getByRole('button', { name: 'Replace document' }))
    await waitFor(() => expect(accept).toHaveBeenCalledWith(5, { hunk: undefined, force: true }))

    await userEvent.click(screen.getByRole('button', { name: 'Reject' }))
    await waitFor(() => expect(reject).toHaveBeenCalledWith(5, undefined))
  })
})

describe('SuggestionPanel: the save barrier before an accept (PLA-289)', () => {
  function renderWithBarrier(
    edit: PendingEdit,
    props: {
      saveBarrier: () => Promise<{ ok: boolean; version: number }>
      onBodyConflict?: (conflict: { serverVersion: number; serverBody: string }) => void
      onApplied?: (result: unknown) => void
    },
  ) {
    const { wrapper } = createWrapper()
    render(
      <SuggestionPanel
        draftId={3}
        edit={edit}
        currentBody="The current document."
        onApplied={props.onApplied ?? vi.fn()}
        saveBarrier={props.saveBarrier}
        onBodyConflict={props.onBodyConflict ?? vi.fn()}
      />,
      { wrapper },
    )
  }

  it('lands the local body first and accepts against the version it confirmed', async () => {
    const accept = vi.spyOn(api, 'acceptPendingEdit').mockResolvedValue({ remaining: 0 })
    const saveBarrier = vi.fn().mockResolvedValue({ ok: true, version: 9 })
    renderWithBarrier(EDIT, { saveBarrier })

    await userEvent.click(screen.getByRole('button', { name: 'Accept all' }))

    await waitFor(() => expect(saveBarrier).toHaveBeenCalled())
    await waitFor(() =>
      expect(accept).toHaveBeenCalledWith(5, {
        hunk: undefined,
        force: false,
        expected_body_version: 9,
      }),
    )
  })

  it('does not accept when the local body could not be saved first', async () => {
    // The barrier reports the current text is not on disk (a save failure, or an open
    // conflict): the suggestion must not be applied over unsaved local writing.
    const accept = vi.spyOn(api, 'acceptPendingEdit').mockResolvedValue({ remaining: 0 })
    const saveBarrier = vi.fn().mockResolvedValue({ ok: false, version: 4 })
    renderWithBarrier(EDIT, { saveBarrier })

    await userEvent.click(screen.getByRole('button', { name: 'Accept all' }))

    await waitFor(() => expect(saveBarrier).toHaveBeenCalled())
    expect(accept).not.toHaveBeenCalled()
  })

  it('feeds a stale-version accept into the save engine rather than a bare toast', async () => {
    vi.spyOn(api, 'acceptPendingEdit').mockRejectedValue(
      new DraftBodyConflictError(409, {
        detail: 'This draft changed somewhere else.',
        code: 'stale_body_version',
        current_version: 12,
        server_body: 'the version saved elsewhere',
      }),
    )
    const onBodyConflict = vi.fn()
    const saveBarrier = vi.fn().mockResolvedValue({ ok: true, version: 7 })
    renderWithBarrier(EDIT, { saveBarrier, onBodyConflict })
    vi.mocked(toast.error).mockClear()

    await userEvent.click(screen.getByRole('button', { name: 'Accept all' }))

    await waitFor(() =>
      expect(onBodyConflict).toHaveBeenCalledWith({
        serverVersion: 12,
        serverBody: 'the version saved elsewhere',
      }),
    )
    // The conflict opens the reconciliation, so no scary toast over the student's words.
    expect(toast.error).not.toHaveBeenCalled()
  })
})
