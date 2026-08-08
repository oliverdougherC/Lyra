import { beforeEach, describe, expect, it, vi } from 'vitest'

import { AgentChatError, ApiError, api, streamChat, streamRegenerate } from '@/lib/api'
import type { ChatEvent } from '@/types'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

/** An SSE body delivered as caller-chosen byte chunks, so frame splitting can be exercised. */
function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder()
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
        controller.close()
      },
    }),
  )
}

/**
 * Stubs `fetch` with a spy that keeps the argument types `lib/api.ts` actually calls it with,
 * so assertions on the recorded call are checked rather than cast.
 */
function mockFetch(response: Response | (() => Promise<Response>)) {
  const impl = typeof response === 'function' ? response : async () => response
  const spy = vi.fn<(url: string, init: RequestInit) => Promise<Response>>(() => impl())
  vi.stubGlobal('fetch', spy)
  return spy
}

beforeEach(() => {
  vi.unstubAllGlobals()
})

describe('request construction', () => {
  it('sends JSON bodies with a JSON content type', async () => {
    const spy = mockFetch(jsonResponse({ id: 1 }))
    await api.createClass({ name: 'Signals', code: 'ECE 203', semester: null })

    const [, init] = spy.mock.calls[0]
    expect(init.method).toBe('POST')
    expect(init.headers).toEqual({ 'content-type': 'application/json' })
    expect(JSON.parse(init.body as string)).toMatchObject({ name: 'Signals' })
  })

  it('uploads a file as form data, letting the browser set the boundary', async () => {
    const spy = mockFetch(jsonResponse({ id: 1 }))
    const file = new File(['x'], 'notes.pdf', { type: 'application/pdf' })
    await api.uploadDocument(1, file)

    const [url, init] = spy.mock.calls[0]
    expect(url).toContain('/api/classes/1/documents')
    // A hand-set content-type here omits the multipart boundary and the upload fails.
    expect(init.headers).toBeUndefined()
    expect(init.body).toBeInstanceOf(FormData)
    expect((init.body as FormData).get('file')).toBe(file)
  })

  it('sends no body or content type on a plain GET', async () => {
    const spy = mockFetch(jsonResponse([]))
    await api.listClasses()

    const [, init] = spy.mock.calls[0]
    expect(init.method).toBe('GET')
    expect(init.body).toBeUndefined()
    expect(init.headers).toBeUndefined()
  })

  it('carries writer depth and plan controls into draft runs', async () => {
    const spy = mockFetch(async () => jsonResponse({ id: 4 }))

    await api.startDraftPass(4, { depth: 'deep', pause_at_plan: true })
    await api.startReview(4, { depth: 'quick' })
    await api.cancelDraftRun(4)

    expect(spy.mock.calls[0][0]).toContain('/api/drafts/4/pass')
    expect(JSON.parse(spy.mock.calls[0][1].body as string)).toEqual({
      depth: 'deep',
      pause_at_plan: true,
    })
    expect(spy.mock.calls[1][0]).toContain('/api/drafts/4/review')
    expect(JSON.parse(spy.mock.calls[1][1].body as string)).toEqual({ depth: 'quick' })
    expect(spy.mock.calls[2][0]).toContain('/api/drafts/4/cancel')
    expect(spy.mock.calls[2][1].method).toBe('POST')
    expect(spy.mock.calls[2][1].body).toBeUndefined()
  })

  it('uses the plan and class source-ledger routes', async () => {
    const spy = mockFetch(async () => jsonResponse([]))

    await api.getDraftPlan(5)
    await api.listDraftSources(9)

    expect(spy.mock.calls[0][0]).toContain('/api/drafts/5/plan')
    expect(spy.mock.calls[1][0]).toContain('/api/classes/9/sources')
  })

  it('reads and updates inheritance-aware class writer settings', async () => {
    const spy = mockFetch(async () => jsonResponse({ overrides: {}, effective: {} }))

    await api.getClassWriterSettings(9)
    await api.updateClassWriterSettings(9, { allow_web_research: null })

    expect(spy.mock.calls[0][0]).toContain('/api/classes/9/writer-settings')
    expect(spy.mock.calls[0][1].method).toBe('GET')
    expect(spy.mock.calls[1][0]).toContain('/api/classes/9/writer-settings')
    expect(spy.mock.calls[1][1].method).toBe('PUT')
    expect(JSON.parse(spy.mock.calls[1][1].body as string)).toEqual({
      allow_web_research: null,
    })
  })

  it('submits one explicit agent profile into the existing class conversation', async () => {
    const spy = mockFetch(async () => jsonResponse({ message_id: 3 }))

    await api.sendAgentChat(9, 12, 'Inspect the parser', 'code')

    expect(spy.mock.calls[0][0]).toContain('/api/classes/9/sessions/12/agent-chat')
    expect(spy.mock.calls[0][1].method).toBe('POST')
    expect(JSON.parse(spy.mock.calls[0][1].body as string)).toEqual({
      content: 'Inspect the parser',
      profile: 'code',
    })
  })
})

describe('error shape', () => {
  it('surfaces a FastAPI string detail', async () => {
    mockFetch(jsonResponse({ detail: 'Class not found.' }, 404))
    await expect(api.getClass(9)).rejects.toThrowError(
      expect.objectContaining({ name: 'ApiError', status: 404, message: 'Class not found.' }),
    )
  })

  it('surfaces the first message of a 422 validation array', async () => {
    mockFetch(jsonResponse({ detail: [{ msg: 'Name must not be empty.' }] }, 422))
    await expect(api.getClass(1)).rejects.toThrowError('Name must not be empty.')
  })

  it('falls back to the status when the error body is not JSON', async () => {
    mockFetch(new Response('<html>502</html>', { status: 502 }))
    await expect(api.getClass(1)).rejects.toThrowError('Request failed with status 502.')
  })

  it('falls back to the status when detail is an empty array', async () => {
    mockFetch(jsonResponse({ detail: [] }, 400))
    await expect(api.getClass(1)).rejects.toThrowError('Request failed with status 400.')
  })

  it('reports a transport failure as status 0 with local-server copy', async () => {
    mockFetch(async () => {
      throw new TypeError('Failed to fetch')
    })
    // The user is told Lyra runs a local server, not shown a raw network error.
    const error = await api.listClasses().catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(0)
    expect((error as ApiError).message).toMatch(/runs locally/)
  })

  it('preserves structured agent-chat failures, including side-effect ids', async () => {
    mockFetch(
      jsonResponse(
        {
          detail: 'The tool loop timed out.',
          retryable: true,
          stopped: 'timeout',
          activity: [
            {
              audit_id: 'audit-1',
              tool: 'search_web',
              capability: 'web',
              effect: 'source',
              state: 'succeeded',
              target_kind: 'source',
              target_id: '17',
            },
          ],
          source_ids: [17],
          workspace_change_ids: [4],
          command_request_ids: [9],
          profile_fact_ids: [12],
        },
        504,
      ),
    )

    const error = await api
      .sendAgentChat(9, 12, 'Inspect the parser', 'research')
      .catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(AgentChatError)
    expect(error).toMatchObject({
      status: 504,
      message: 'The tool loop timed out.',
      retryable: true,
      stopped: 'timeout',
      source_ids: [17],
      workspace_change_ids: [4],
      command_request_ids: [9],
      profile_fact_ids: [12],
    })
  })
})

describe('abort handling', () => {
  it('rethrows an abort rather than disguising it as unreachable', async () => {
    // A cancelled query must not surface to the user as "the server is down".
    mockFetch(async () => {
      throw new DOMException('The operation was aborted.', 'AbortError')
    })
    const error = await api.listClasses().catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(DOMException)
    expect((error as DOMException).name).toBe('AbortError')
  })

  it('passes the caller signal through to fetch', async () => {
    const spy = mockFetch(jsonResponse([]))
    const controller = new AbortController()
    await api.listClasses(controller.signal)

    const [, init] = spy.mock.calls[0]
    expect(init.signal).toBe(controller.signal)
  })
})

describe('SSE frame parsing', () => {
  it('emits one event per data frame', async () => {
    mockFetch(
      sseResponse([
        'data: {"type":"token","value":"He"}\n',
        'data: {"type":"token","value":"llo"}\n',
        'data: {"type":"done"}\n',
      ]),
    )

    const events: ChatEvent[] = []
    await streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, (e) => events.push(e))

    expect(events).toEqual([
      { type: 'token', value: 'He' },
      { type: 'token', value: 'llo' },
      { type: 'done' },
    ])
  })

  it('buffers a frame split across chunk boundaries', async () => {
    // The reader hands back arbitrary byte runs, so a frame can arrive in pieces.
    mockFetch(sseResponse(['data: {"type":"tok', 'en","value":"split"}\n']))

    const events: ChatEvent[] = []
    await streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, (e) => events.push(e))

    expect(events).toEqual([{ type: 'token', value: 'split' }])
  })

  it('handles several frames arriving in one chunk', async () => {
    mockFetch(
      sseResponse(['data: {"type":"token","value":"a"}\ndata: {"type":"token","value":"b"}\n']),
    )

    const events: ChatEvent[] = []
    await streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, (e) => events.push(e))

    expect(events).toHaveLength(2)
  })

  it('keeps reasoning frames distinct from answer frames', async () => {
    // A thought must never be mixed into the tokens that carry the reply.
    mockFetch(
      sseResponse([
        'data: {"type":"reasoning","value":"thinking"}\n',
        'data: {"type":"token","value":"answer"}\n',
      ]),
    )

    const events: ChatEvent[] = []
    await streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, (e) => events.push(e))

    expect(events.map((event) => event.type)).toEqual(['reasoning', 'token'])
  })

  it('drops an unparseable frame instead of killing the stream', async () => {
    mockFetch(
      sseResponse([
        'data: {"type":"token","value":"one"}\n',
        'data: not json at all\n',
        'data: {"type":"token","value":"two"}\n',
      ]),
    )

    const events: ChatEvent[] = []
    await streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, (e) => events.push(e))

    expect(events).toHaveLength(2)
  })

  it('ignores SSE comments and non-data lines', async () => {
    mockFetch(sseResponse([': keep-alive\n', 'event: message\n', 'data: {"type":"done"}\n']))

    const events: ChatEvent[] = []
    await streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, (e) => events.push(e))

    expect(events).toEqual([{ type: 'done' }])
  })

  it('raises the backend error rather than opening a reader', async () => {
    mockFetch(jsonResponse({ detail: 'No endpoint configured.' }, 400))
    await expect(
      streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, () => {}),
    ).rejects.toThrowError('No endpoint configured.')
  })
})

describe('regenerate', () => {
  it('posts to the regenerate route and carries no question', async () => {
    // Retry means answer again, not ask again: the question is already stored.
    const spy = mockFetch(sseResponse(['data: {"type":"done"}\n']))
    await streamRegenerate(7, { mode: 'show', document_id: null }, () => {})

    const [url, init] = spy.mock.calls[0]
    expect(url).toContain('/api/sessions/7/regenerate')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).not.toHaveProperty('content')
  })
})

describe('PDF export', () => {
  it('returns the response as a blob and posts to the export route', async () => {
    const spy = mockFetch(
      new Response(new Uint8Array([37, 80, 68, 70]), {
        headers: { 'content-type': 'application/pdf' },
      }),
    )

    const pdf = await api.exportDraftPdf(4)

    const [url, init] = spy.mock.calls[0]
    expect(url).toContain('/api/drafts/4/export')
    expect(init.method).toBe('POST')
    expect(pdf.size).toBe(4)
  })

  it('surfaces a missing binary as the backend message', async () => {
    mockFetch(jsonResponse({ detail: 'PDF export needs typst.' }, 400))

    await expect(api.exportDraftPdf(4)).rejects.toThrowError('PDF export needs typst.')
  })
})
