/**
 * Workspace regressions for the body-dependent actions, driven through the real save engine
 * and the real `api.updateDraftBody` (fetch stubbed at the network boundary). They lock the
 * cross-operation invariants the draft page relies on (PLA-289):
 *
 * - A body-dependent action (start a pass, a review, an export, a snapshot) proves the newest
 *   local body is on disk before it runs. A save failure or a conflict leaves it unstarted.
 * - A snapshot that conflicts on its compare-and-swap enters the same reconciliation the
 *   autosave uses: the local text is kept, the indicator is never left saying Saved.
 *
 * The harness wires the engine to the page's own `ensureBodySaved`/`onSnapshot` shapes so the
 * test exercises the real coordination rather than a mock of it.
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useRef, useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SaveStateIndicator } from '@/components/drafts/save-state-indicator'
import { api, DraftBodyConflictError } from '@/lib/api'
import { createSaveEngine, type SaveConflict, type SaveStateName } from '@/lib/drafts/save-engine'

const DRAFT_ID = 7

function jsonResponse(status: number, payload: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as Response
}

function Harness({ onStartPass, log }: { onStartPass: () => void; log: SaveStateName[] }) {
  const [state, setState] = useState<SaveStateName>('saved')
  const [detail, setDetail] = useState<string | null>(null)
  const bodyRef = useRef('the seed body')
  const [engine] = useState(() => {
    const created = createSaveEngine({
      write: (content, expectedVersion) =>
        api
          .updateDraftBody(DRAFT_ID, { content, expected_version: expectedVersion })
          .then((result) => ({ version: result.version })),
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
    created.noteSaved('the seed body', 3)
    return created
  })

  // The page's own barrier and snapshot, replicated so the test drives the real logic.
  const ensureBodySaved = async () => (await engine.flush(bodyRef.current)).ok

  async function startPass() {
    if (!(await ensureBodySaved())) return
    onStartPass()
  }

  async function snapshot() {
    if (!(await ensureBodySaved())) return
    try {
      const result = await api.updateDraftBody(DRAFT_ID, {
        content: bodyRef.current,
        expected_version: engine.version(),
        snapshot: true,
      })
      engine.noteSaved(bodyRef.current, result.version)
      setState('saved')
    } catch (caught) {
      if (caught instanceof DraftBodyConflictError) {
        engine.forceConflict(caught.serverBody, caught.currentVersion)
        return
      }
      throw caught
    }
  }

  return (
    <div>
      <SaveStateIndicator state={state} detail={detail} />
      <button
        type="button"
        onClick={() => {
          bodyRef.current = 'the newest writing'
          engine.schedule('the newest writing')
        }}
      >
        type
      </button>
      <button type="button" onClick={() => void startPass()}>
        pass
      </button>
      <button type="button" onClick={() => void snapshot()}>
        snapshot
      </button>
    </div>
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('body-dependent actions require the newest body to be on disk', () => {
  it('does not start a pass when the flush fails, and shows the save error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(500, { detail: 'The server broke.' })),
    )
    const onStartPass = vi.fn()
    const log: SaveStateName[] = []
    render(<Harness onStartPass={onStartPass} log={log} />)

    await userEvent.click(screen.getByRole('button', { name: 'type' }))
    await userEvent.click(screen.getByRole('button', { name: 'pass' }))

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Could not save'))
    // The pass never started over a body the server does not hold.
    expect(onStartPass).not.toHaveBeenCalled()
    expect(log.at(-1)).toBe('error')
  })

  it('does not start a pass when the flush conflicts, and opens the reconciliation state', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(409, {
          detail: 'This draft changed somewhere else.',
          code: 'stale_body_version',
          current_version: 9,
          server_body: 'the body saved elsewhere',
        }),
      ),
    )
    const onStartPass = vi.fn()
    const log: SaveStateName[] = []
    render(<Harness onStartPass={onStartPass} log={log} />)

    await userEvent.click(screen.getByRole('button', { name: 'type' }))
    await userEvent.click(screen.getByRole('button', { name: 'pass' }))

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Changed elsewhere'))
    expect(onStartPass).not.toHaveBeenCalled()
    expect(log).not.toContain('saved')
  })

  it('a snapshot that loses the CAS enters reconciliation and never leaves Saved showing', async () => {
    // The flush write lands; the snapshot's compare-and-swap then loses to another tab.
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as {
        snapshot?: boolean
        expected_version: number
      }
      if (body.snapshot) {
        return jsonResponse(409, {
          detail: 'This draft changed somewhere else.',
          code: 'stale_body_version',
          current_version: 12,
          server_body: 'a newer body from another tab',
        })
      }
      return jsonResponse(200, { part_id: 1, saved: true, version: body.expected_version + 1 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const log: SaveStateName[] = []
    render(<Harness onStartPass={vi.fn()} log={log} />)

    await userEvent.click(screen.getByRole('button', { name: 'type' }))
    await userEvent.click(screen.getByRole('button', { name: 'snapshot' }))

    // The snapshot conflict is fed into the engine: the indicator ends on the conflict, not
    // on a false Saved over a body the server no longer has.
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Changed elsewhere'))
    expect(screen.getByRole('status')).not.toHaveTextContent('Saved')
    expect(log.at(-1)).toBe('conflict')
  })
})
