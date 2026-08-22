/**
 * The equality/in-flight sync race, driven through the real reachable UI path (PLA-289,
 * review 5000931997).
 *
 * A body-neutral suggestion Reject is the concrete way `syncEditorFromServer` runs while an
 * autosave is still in flight: reject writes no body, its success calls `onApplied`, and the
 * workspace reconciles from a refetch that still holds the pre-edit body. If the editor has
 * been reverted to that same body, the freshly-read bytes equal the editor's - and the old
 * code adopted that baseline with `noteSaved`, bumping the write epoch and reporting Saved
 * while the in-flight autosave was still free to commit its own (different) body server-side.
 *
 * This renders the real `SuggestionPanel` (its real `useRejectEdit` mutation) wired to the
 * real save engine and a faithful copy of the workspace's `syncEditorFromServer`. The body
 * write is a deferred compare-and-swap server so the A autosave can be held in flight across
 * the reject, deterministically. The indicator must never settle on Saved while the server
 * holds A; the two must converge on S.
 */
import { QueryClient, QueryClientProvider, useQueryClient } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SaveStateIndicator } from '@/components/drafts/save-state-indicator'
import { SuggestionPanel } from '@/components/drafts/suggestion-panel'
import { api, DraftBodyConflictError } from '@/lib/api'
import {
  createSaveEngine,
  decideServerSync,
  type SaveConflict,
  type SaveStateName,
  type WriteOutcome,
} from '@/lib/drafts/save-engine'
import { draftKeys } from '@/lib/hooks/use-drafts'
import type { AcceptRejectResult, DraftDetail, PendingEdit } from '@/types'

const DRAFT_ID = 7
const EDIT_ID = 55

/** A pending suggestion with one hunk, not stale, so the panel offers "Reject all". */
const EDIT: PendingEdit = {
  id: EDIT_ID,
  stale: false,
  note: 'Tighten the opening',
  proposed_content: 'A',
  hunks: [
    {
      index: 0,
      old_start: 0,
      old_lines: 1,
      new_start: 0,
      new_lines: 1,
      lines: [' context', '-S', '+A'],
      hash: 'h0',
    },
  ],
}

/** A compare-and-swap body server whose writes settle only when the test says so. */
class DeferredServer {
  private queue: Array<{
    content: string
    expected: number
    resolve: (outcome: WriteOutcome) => void
    reject: (error: unknown) => void
  }> = []

  constructor(
    public body = '',
    public version = 0,
  ) {}

  write = (content: string, expected: number): Promise<WriteOutcome> =>
    new Promise<WriteOutcome>((resolve, reject) => {
      this.queue.push({ content, expected, resolve, reject })
    })

  inFlight(): number {
    return this.queue.length
  }

  async settleNext(): Promise<void> {
    const job = this.queue.shift()
    if (!job) throw new Error('no write in flight to settle')
    if (job.expected !== this.version) {
      job.reject(
        new DraftBodyConflictError(409, {
          detail: 'This draft changed somewhere else.',
          code: 'stale_body_version',
          current_version: this.version,
          server_body: this.body,
        }),
      )
    } else {
      this.body = job.content
      this.version += 1
      job.resolve({ version: this.version })
    }
    // Let the engine's awaited continuations run.
    for (let i = 0; i < 5; i += 1) await Promise.resolve()
  }
}

function Harness({
  server,
  log,
  bodyRef,
}: {
  server: DeferredServer
  log: SaveStateName[]
  bodyRef: { current: string }
}) {
  const queryClient = useQueryClient()
  const [state, setState] = useState<SaveStateName>('saved')
  const [detail, setDetail] = useState<string | null>(null)
  const [engine] = useState(() => {
    const created = createSaveEngine({
      write: (content, expectedVersion) => server.write(content, expectedVersion),
      onState: (next, nextDetail) => {
        log.push(next)
        setState(next)
        setDetail(nextDetail ?? null)
      },
      isConflict: (error): SaveConflict | null =>
        error instanceof DraftBodyConflictError
          ? { serverVersion: error.currentVersion, serverBody: error.serverBody }
          : null,
      debounceMs: 5,
    })
    created.noteSaved('S', 0)
    return created
  })

  // A faithful copy of the workspace's syncEditorFromServer reconciliation (no math
  // normalization in the test, so `seeded === fresh.body`).
  async function syncEditorFromServer() {
    await queryClient.invalidateQueries({ queryKey: draftKeys.detail(DRAFT_ID) })
    const fresh = queryClient.getQueryData<DraftDetail>(draftKeys.detail(DRAFT_ID))
    if (!fresh) return
    const localBody = bodyRef.current
    const editorShowsServer = localBody === fresh.body
    const decision = decideServerSync(engine, localBody, editorShowsServer)
    if (decision === 'skip') return
    if (decision === 'adopt') {
      engine.noteSaved(fresh.body, fresh.body_version)
      bodyRef.current = fresh.body
      setState('saved')
      return
    }
    engine.forceConflict(fresh.body, fresh.body_version)
  }

  const onApplied = () => {
    void syncEditorFromServer()
  }

  return (
    <div>
      <SaveStateIndicator state={state} detail={detail} />
      <button
        type="button"
        onClick={() => {
          bodyRef.current = 'A'
          engine.schedule('A')
        }}
      >
        type
      </button>
      <button
        type="button"
        onClick={() => {
          bodyRef.current = 'S'
          engine.schedule('S')
        }}
      >
        revert
      </button>
      <SuggestionPanel
        draftId={DRAFT_ID}
        edit={EDIT}
        // Unused on the non-stale reject path (only the side-by-side stale view reads it).
        currentBody="S"
        onApplied={onApplied}
        onBodyConflict={(conflict) =>
          engine.forceConflict(conflict.serverBody, conflict.serverVersion)
        }
      />
    </div>
  )
}

function seededClient(): QueryClient {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  client.setQueryData(draftKeys.detail(DRAFT_ID), {
    part_id: 1,
    body: 'S',
    body_version: 0,
    pending: true,
  } as unknown as DraftDetail)
  return client
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('a suggestion reject while an autosave is in flight never falsely reports Saved', () => {
  it('holds the in-flight save contract across the reject and converges editor and server on S', async () => {
    const server = new DeferredServer('S', 0)
    const bodyRef = { current: 'S' }
    const log: SaveStateName[] = []
    // The reject writes no body; it just answers that nothing is left of the edit.
    const rejectSpy = vi
      .spyOn(api, 'rejectPendingEdit')
      .mockResolvedValue({ remaining: 0 } satisfies AcceptRejectResult)

    const client = seededClient()
    render(
      <QueryClientProvider client={client}>
        <Harness server={server} log={log} bodyRef={bodyRef} />
      </QueryClientProvider>,
    )

    // Type A and let the autosave reach the wire, held in flight by the deferred server.
    await userEvent.click(screen.getByRole('button', { name: 'type' }))
    await waitFor(() => expect(server.inFlight()).toBe(1))
    expect(screen.getByRole('status')).toHaveTextContent('Saving')

    // Undo back to S while A is still in the air; a corrective S write is now owed.
    await userEvent.click(screen.getByRole('button', { name: 'revert' }))

    // Reject the suggestion before A settles. Reject is body-neutral, so the refetch still
    // reads S@v0 and the editor already shows S - the exact equality/in-flight collision.
    await userEvent.click(screen.getByRole('button', { name: 'Reject all' }))
    await waitFor(() => expect(rejectSpy).toHaveBeenCalled())

    // The sync must not have adopted a baseline or reported Saved: A can still commit.
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saving'))
    expect(screen.getByRole('status')).not.toHaveTextContent('Saved')
    expect(log).not.toContain('saved')

    // A now commits A@v1 server-side. Its response is threaded through the engine, which
    // still owes the corrective S write - so the indicator stays off Saved.
    await act(async () => {
      await server.settleNext()
    })
    expect(server.body).toBe('A')
    expect(server.version).toBe(1)
    expect(screen.getByRole('status')).not.toHaveTextContent('Saved')
    expect(log).not.toContain('saved')

    // The corrective write of S goes out next and converges editor and server on S@v2.
    await waitFor(() => expect(server.inFlight()).toBe(1))
    await act(async () => {
      await server.settleNext()
    })

    expect(server.body).toBe('S')
    expect(server.version).toBe(2)
    expect(bodyRef.current).toBe('S')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved'))
    // Saved appeared only at the end, once the server actually held S again.
    expect(log.at(-1)).toBe('saved')
  })
})
