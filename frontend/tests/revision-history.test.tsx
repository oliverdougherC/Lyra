/**
 * The draft-body branch of the shared revision history: restoring is version-aware, so a
 * stale tab cannot silently replace a body that changed elsewhere, and the student's current
 * text is confirmed on disk first so restoring an older version loses nothing (PLA-289).
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { toast } from 'sonner'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { RevisionHistory } from '@/components/solutions/revision-history'
import { api, DraftBodyConflictError } from '@/lib/api'
import type { SolutionPart, SolutionRevision } from '@/types'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return Wrapper
}

const PART: SolutionPart = {
  id: 11,
  artifact_id: 3,
  parent_part_id: null,
  kind: 'draft_body',
  ordinal: 1,
  label: 'Draft',
  content: 'the newest body',
  content_type: 'markdown',
  status: 'complete',
  origin: 'user_corrected',
  verdict: 'unchecked',
  verdict_detail: null,
  solve_parts: 'together',
  error_message: null,
  provenance: [],
  checks: [],
}

// Newest first: the first row is "shown now" (no Restore); the older row carries the button.
const REVISIONS: SolutionRevision[] = [
  {
    revision: 2,
    content: 'the newest body',
    origin: 'user_corrected',
    note: null,
    created_at: '2026-08-20T00:00:00Z',
  },
  {
    revision: 1,
    content: 'an earlier draft',
    origin: 'generated',
    note: null,
    created_at: '2026-08-19T00:00:00Z',
  },
]

function restoredPart(): SolutionPart {
  return { ...PART, content: 'an earlier draft' }
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(toast.success).mockClear()
  vi.mocked(toast.error).mockClear()
  vi.spyOn(api, 'listPartRevisions').mockResolvedValue(REVISIONS)
})

describe('RevisionHistory: version-aware draft restore', () => {
  it('confirms the current body first and restores against the version it reports', async () => {
    const restore = vi.spyOn(api, 'restorePartRevision').mockResolvedValue(restoredPart())
    const saveBeforeRestore = vi.fn().mockResolvedValue({ ok: true, version: 5 })
    const onClose = vi.fn()
    const wrapper = createWrapper()

    render(
      <RevisionHistory
        artifactId={3}
        part={PART}
        noun="draft"
        onClose={onClose}
        saveBeforeRestore={saveBeforeRestore}
        onBodyConflict={vi.fn()}
      />,
      { wrapper },
    )

    await userEvent.click(await screen.findByRole('button', { name: 'Restore' }))

    await waitFor(() => expect(saveBeforeRestore).toHaveBeenCalled())
    // The version the barrier confirmed is what the restore is guarded against.
    await waitFor(() => expect(restore).toHaveBeenCalledWith(3, 11, 1, 5))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('does not restore when the current body could not be saved first', async () => {
    const restore = vi.spyOn(api, 'restorePartRevision').mockResolvedValue(restoredPart())
    const saveBeforeRestore = vi.fn().mockResolvedValue({ ok: false, version: 4 })
    const wrapper = createWrapper()

    render(
      <RevisionHistory
        artifactId={3}
        part={PART}
        noun="draft"
        onClose={vi.fn()}
        saveBeforeRestore={saveBeforeRestore}
        onBodyConflict={vi.fn()}
      />,
      { wrapper },
    )

    await userEvent.click(await screen.findByRole('button', { name: 'Restore' }))

    await waitFor(() => expect(saveBeforeRestore).toHaveBeenCalled())
    expect(restore).not.toHaveBeenCalled()
  })

  it('feeds a stale-version restore into the save engine instead of overwriting', async () => {
    vi.spyOn(api, 'restorePartRevision').mockRejectedValue(
      new DraftBodyConflictError(409, {
        detail: 'This draft changed somewhere else.',
        code: 'stale_body_version',
        current_version: 8,
        server_body: 'the body changed elsewhere',
      }),
    )
    const onBodyConflict = vi.fn()
    const onClose = vi.fn()
    const wrapper = createWrapper()

    render(
      <RevisionHistory
        artifactId={3}
        part={PART}
        noun="draft"
        onClose={onClose}
        saveBeforeRestore={vi.fn().mockResolvedValue({ ok: true, version: 2 })}
        onBodyConflict={onBodyConflict}
      />,
      { wrapper },
    )

    await userEvent.click(await screen.findByRole('button', { name: 'Restore' }))

    await waitFor(() =>
      expect(onBodyConflict).toHaveBeenCalledWith({
        serverVersion: 8,
        serverBody: 'the body changed elsewhere',
      }),
    )
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('restores unchanged when no barrier is supplied (the solution history path)', async () => {
    const restore = vi.spyOn(api, 'restorePartRevision').mockResolvedValue(restoredPart())
    const wrapper = createWrapper()

    render(<RevisionHistory artifactId={3} part={PART} onClose={vi.fn()} />, { wrapper })

    await userEvent.click(await screen.findByRole('button', { name: 'Restore' }))

    // No expected version is sent, so the solution surfaces keep restoring unconditionally.
    await waitFor(() => expect(restore).toHaveBeenCalledWith(3, 11, 1, undefined))
  })
})

describe('RevisionHistory: the "shown now" marker is honest for drafts', () => {
  it('marks the row that matches the live editor body as shown now', async () => {
    const wrapper = createWrapper()

    render(
      <RevisionHistory
        artifactId={3}
        part={PART}
        noun="draft"
        onClose={vi.fn()}
        saveBeforeRestore={vi.fn().mockResolvedValue({ ok: true, version: 2 })}
        onBodyConflict={vi.fn()}
        // The editor is showing the newest recorded revision.
        currentBody="the newest body"
      />,
      { wrapper },
    )

    // The newest row is current, so it carries the marker and no Restore button; only the
    // older row can be restored.
    expect(await screen.findByText('This is what is shown now.')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Restore' })).toHaveLength(1)
  })

  it('claims no row when the live body is newer than every recorded revision (autosave)', async () => {
    const wrapper = createWrapper()

    render(
      <RevisionHistory
        artifactId={3}
        part={PART}
        noun="draft"
        onClose={vi.fn()}
        saveBeforeRestore={vi.fn().mockResolvedValue({ ok: true, version: 2 })}
        onBodyConflict={vi.fn()}
        // The student typed past the last snapshot; the live body was only autosaved, so it is
        // not among the recorded revisions.
        currentBody="a body typed past the last saved revision"
      />,
      { wrapper },
    )

    await screen.findByText('an earlier draft')
    // No row may claim to be current, because none of them is; every recorded version - the
    // newest saved one included - is offered for restore instead.
    expect(screen.queryByText('This is what is shown now.')).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Restore' })).toHaveLength(2)
  })
})
