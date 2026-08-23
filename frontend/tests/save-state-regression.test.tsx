/**
 * A component regression around the *visible* save status, driven through the real API
 * layer rather than a mocked save-engine callback. It wires the real `createSaveEngine` to
 * the real `api.updateDraftBody` (fetch is stubbed at the network boundary, so the request
 * shape, the version echo, and the 409 -> `DraftBodyConflictError` parsing all run) and
 * renders the real `SaveStateIndicator`. It proves the indicator can never say `Saved`
 * while the backend holds an older body (PLA-289).
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SaveStateIndicator } from '@/components/drafts/save-state-indicator'
import { api, DraftBodyConflictError } from '@/lib/api'
import { createSaveEngine, type SaveConflict, type SaveStateName } from '@/lib/drafts/save-engine'

const DRAFT_ID = 7

function Harness({ seedVersion, log }: { seedVersion: number; log: SaveStateName[] }) {
  const [state, setState] = useState<SaveStateName>('saved')
  const [detail, setDetail] = useState<string | null>(null)
  // Created once, the same lazy-init pattern the workspace uses for its save engine.
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
    created.noteSaved('the seed body', seedVersion)
    return created
  })
  return (
    <div>
      <SaveStateIndicator state={state} detail={detail} />
      <button type="button" onClick={() => engine.schedule('the newest writing')}>
        type
      </button>
    </div>
  )
}

function jsonResponse(status: number, payload: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as Response
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('save status regression through the real API layer', () => {
  it('shows Saved only after the server confirms the write, carrying the version', async () => {
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body))
      expect(body).toEqual({ content: 'the newest writing', expected_version: 3 })
      return jsonResponse(200, { part_id: 1, saved: true, version: 4 })
    })
    vi.stubGlobal('fetch', fetchMock)

    const log: SaveStateName[] = []
    render(<Harness seedVersion={3} log={log} />)

    await userEvent.click(screen.getByRole('button', { name: 'type' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved'))

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(log).toEqual(['dirty', 'saving', 'saved'])
  })

  it('never shows Saved when the server refuses a stale body, and reveals the conflict', async () => {
    // The backend already moved to version 4 with newer text; this editor still thinks it
    // is at version 3, so its write is refused with a deterministic 409.
    const fetchMock = vi.fn(async () =>
      jsonResponse(409, {
        detail: 'This draft changed somewhere else, so your latest edit was not saved yet.',
        code: 'stale_body_version',
        current_version: 4,
        server_body: 'the body the server actually holds',
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const log: SaveStateName[] = []
    render(<Harness seedVersion={3} log={log} />)

    await userEvent.click(screen.getByRole('button', { name: 'type' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Changed elsewhere'))

    // The visible status went dirty -> saving -> conflict and never once said Saved: the
    // indicator cannot claim the words are on disk while the server holds an older body.
    expect(log).toEqual(['dirty', 'saving', 'conflict'])
    expect(log).not.toContain('saved')
    expect(screen.getByRole('status')).not.toHaveTextContent('Saved')
  })
})
