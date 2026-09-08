import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  AgentChatError,
  ApiError,
  api,
  streamChat,
  streamRegenerate,
  streamWrite,
  streamWriterChat,
} from '@/lib/api'
import { SseStreamError } from '@/lib/sse'
import type { ChatEvent, WriteEvent } from '@/types'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

/** An SSE body delivered as caller-chosen byte chunks, so frame splitting can be exercised. */
function sseResponse(chunks: string[], hooks?: { onCancel?: () => void }): Response {
  const encoder = new TextEncoder()
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
        controller.close()
      },
      cancel: hooks?.onCancel,
    }),
  )
}

/** The raw bytes of a stream cut at a caller-chosen offset, so multibyte splits can be exercised. */
function sseBytesResponse(chunks: Uint8Array[]): Response {
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(chunk)
        controller.close()
      },
    }),
  )
}

/** Slice `bytes` at the caller-chosen offsets; -1 means the whole payload in one chunk. */
function inChunks(bytes: Uint8Array, sizes: number[]): Uint8Array[] {
  if (sizes.length === 1 && sizes[0] === -1) return [bytes]
  const out: Uint8Array[] = []
  let offset = 0
  for (const size of sizes) {
    out.push(bytes.subarray(offset, offset + size))
    offset += size
  }
  if (offset < bytes.length) out.push(bytes.subarray(offset))
  return out
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
  window.__LYRA_BOOTSTRAP__ = { apiBase: 'http://127.0.0.1:8000' }
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

  it('uses the live draft suggestion routes and carries revision tokens', async () => {
    const spy = mockFetch(async () => jsonResponse({ id: 14, blocks: [] }))

    await api.getLiveDraftSuggestion(5)
    await api.updateLiveDraftSuggestionBlock(5, 101, {
      content: 'Edited block',
      expected_revision: 7,
      base_content: 'Original block',
    })
    await api.finalizeLiveDraftSuggestion(5)

    expect(spy.mock.calls[0][0]).toContain('/api/drafts/5/live-suggestion')
    expect(spy.mock.calls[0][1].method).toBe('GET')
    expect(spy.mock.calls[1][0]).toContain('/api/drafts/5/live-suggestion/blocks/101')
    expect(spy.mock.calls[1][1].method).toBe('PATCH')
    expect(JSON.parse(spy.mock.calls[1][1].body as string)).toEqual({
      content: 'Edited block',
      expected_revision: 7,
      base_content: 'Original block',
    })
    expect(spy.mock.calls[2][0]).toContain('/api/drafts/5/live-suggestion/finalize')
    expect(spy.mock.calls[2][1].method).toBe('POST')
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

describe('agent chat idempotency and stop (PLA-313)', () => {
  it('carries the browser operation ID into the agent turn', async () => {
    const spy = mockFetch(async () => jsonResponse({ message_id: 3 }))
    const signal = new AbortController().signal

    await api.sendAgentChat(9, 12, 'Inspect the parser', undefined, 5, 'show', 'op-abc', signal)

    const [url, init] = spy.mock.calls[0]
    expect(url).toContain('/api/classes/9/sessions/12/agent-chat')
    expect(init.signal).toBe(signal)
    expect(JSON.parse(init.body as string)).toEqual({
      content: 'Inspect the parser',
      document_id: 5,
      mode: 'show',
      operation_id: 'op-abc',
    })
  })

  it('omits the operation ID from the body when the browser minted none', async () => {
    const spy = mockFetch(async () => jsonResponse({ message_id: 3 }))

    await api.sendAgentChat(9, 12, 'Inspect the parser')

    expect(JSON.parse(spy.mock.calls[0][1].body as string)).toEqual({
      content: 'Inspect the parser',
    })
  })

  it('sends the current selection into a manual regeneration and nothing into a body-less one', async () => {
    const spy = mockFetch(async () => jsonResponse({ message_id: 3 }))

    await api.regenerateAgentChat(9, 12, { mode: 'show', documentId: 5 })
    await api.regenerateAgentChat(9, 12)

    const [manualUrl, manualInit] = spy.mock.calls[0]
    expect(manualUrl).toContain('/agent-chat/regenerate')
    expect(JSON.parse(manualInit.body as string)).toEqual({ mode: 'show', document_id: 5 })

    const [, continuationInit] = spy.mock.calls[1]
    expect(continuationInit.body).toBeUndefined()
  })

  it('carries an explicit null document (All material) into retry and regeneration', async () => {
    // PLA-401 final pass: a null documentId is the real value "All material", so it must
    // ride the wire as an EXPLICIT null (property presence, not non-nullness) - the server
    // reads the persisted scope only when the caller did not name one. An absent property
    // is the other message: continue the stored scope (the body-less JIT continuation).
    const spy = mockFetch(async () => jsonResponse({ message_id: 3 }))

    await api.retryAgentChat(9, 12, { documentId: null })
    await api.regenerateAgentChat(9, 12, { mode: 'show', documentId: null })
    await api.regenerateAgentChat(9, 12, { mode: 'show' })

    const [, retryInit] = spy.mock.calls[0]
    expect(JSON.parse(retryInit.body as string)).toEqual({ document_id: null })
    const [, manualInit] = spy.mock.calls[1]
    expect(JSON.parse(manualInit.body as string)).toEqual({ mode: 'show', document_id: null })
    const [, absentInit] = spy.mock.calls[2]
    expect(JSON.parse(absentInit.body as string)).toEqual({ mode: 'show' })
    expect(JSON.parse(absentInit.body as string)).not.toHaveProperty('document_id')
  })

  it('stops the in-flight agent turn through its explicit endpoint', async () => {
    const spy = mockFetch(async () => jsonResponse({ stopped: true }))

    const result = await api.stopAgentChat(9, 12)

    expect(spy.mock.calls[0][0]).toContain('/api/classes/9/sessions/12/agent-chat/stop')
    expect(spy.mock.calls[0][1].method).toBe('POST')
    expect(result).toEqual({ stopped: true })
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

  it('passes the caller signal through to protected asset loads', async () => {
    vi.resetModules()
    window.__LYRA_BOOTSTRAP__ = {
      apiBase: 'http://127.0.0.1:8000',
      sessionHeader: 'secret-session',
    }
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn(() => 'blob:page'),
    })
    Object.defineProperty(URL, 'revokeObjectURL', {
      configurable: true,
      value: vi.fn(),
    })
    const spy = mockFetch(new Response('page', { status: 200 }))
    const controller = new AbortController()
    const { loadProtectedAssetSource } = await import('@/lib/api')

    const asset = await loadProtectedAssetSource('/api/documents/7/pages/1', controller.signal)
    asset.release?.()

    const [, init] = spy.mock.calls[0]
    expect(init.signal).toBe(controller.signal)
  })

  it('treats a WebKit TypeError from an already-aborted signal as cancellation', async () => {
    const controller = new AbortController()
    controller.abort()
    mockFetch(async () => {
      throw new TypeError('Load failed')
    })

    const error = await api.listClasses(controller.signal).catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(TypeError)
    expect(error).not.toBeInstanceOf(ApiError)
  })
})

describe('SSE frame parsing', () => {
  /** One real wire-protocol frame: a JSON object on a `data:` line, then the blank line. */
  const frame = (value: object) => `data: ${JSON.stringify(value)}\n\n`

  it('emits one event per data frame', async () => {
    mockFetch(
      sseResponse([
        frame({ type: 'start', message_id: 11 }),
        frame({ type: 'token', text: 'He' }),
        frame({ type: 'token', text: 'llo' }),
        frame({ type: 'done', message_id: 11 }),
      ]),
    )

    const events: ChatEvent[] = []
    await streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, (e) => events.push(e))

    expect(events).toEqual([
      { type: 'start', message_id: 11 },
      { type: 'token', text: 'He' },
      { type: 'token', text: 'llo' },
      { type: 'done', message_id: 11 },
    ])
  })

  it('buffers a frame split across chunk boundaries', async () => {
    // The reader hands back arbitrary byte runs, so a frame can arrive in pieces.
    mockFetch(
      sseResponse([
        'data: {"type":"tok',
        `en","text":"split"}\n\n${frame({ type: 'done', message_id: 1 })}`,
      ]),
    )

    const events: ChatEvent[] = []
    await streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, (e) => events.push(e))

    expect(events).toEqual([
      { type: 'token', text: 'split' },
      { type: 'done', message_id: 1 },
    ])
  })

  it('handles several frames arriving in one chunk', async () => {
    mockFetch(
      sseResponse([
        frame({ type: 'token', text: 'a' }) + frame({ type: 'token', text: 'b' }),
        frame({ type: 'done', message_id: 1 }),
      ]),
    )

    const events: ChatEvent[] = []
    await streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, (e) => events.push(e))

    expect(events).toHaveLength(3)
  })

  it('keeps reasoning frames distinct from answer frames', async () => {
    // A thought must never be mixed into the tokens that carry the reply.
    mockFetch(
      sseResponse([
        frame({ type: 'reasoning', text: 'thinking' }),
        frame({ type: 'token', text: 'answer' }),
        frame({ type: 'done', message_id: 1 }),
      ]),
    )

    const events: ChatEvent[] = []
    await streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, (e) => events.push(e))

    expect(events.map((event) => event.type)).toEqual(['reasoning', 'token', 'done'])
  })

  it('ignores SSE comments, metadata fields, and empty data lines', async () => {
    mockFetch(
      sseResponse([
        ': keep-alive\n',
        'event: message\nid: 7\nretry: 3000\nnote: hello\n',
        'data:\n\n',
        frame({ type: 'done', message_id: 1 }),
      ]),
    )

    const events: ChatEvent[] = []
    await streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, (e) => events.push(e))

    expect(events).toEqual([{ type: 'done', message_id: 1 }])
  })

  it('delivers a final frame missing only its blank-line terminator', async () => {
    // The legacy readers consumed a complete final payload whose terminator was never
    // flushed; the framing layer keeps that leniency (only a CUT payload fails).
    mockFetch(sseResponse([frame({ type: 'token', text: 'He' }), 'data: {"type":"done"}']))

    const events: ChatEvent[] = []
    await streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, (e) => events.push(e))

    expect(events).toEqual([{ type: 'token', text: 'He' }, { type: 'done' }])
  })

  it('rejects a malformed frame instead of dropping it', async () => {
    mockFetch(
      sseResponse([
        frame({ type: 'token', text: 'one' }),
        'data: not json at all\n\n',
        frame({ type: 'done', message_id: 1 }),
      ]),
    )

    const events: ChatEvent[] = []
    const pending = streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, (e) =>
      events.push(e),
    )
    const error = await pending.catch((caught: unknown) => caught)

    // The frame fails deliberately with a bounded message - no raw payload echoed.
    expect(error).toBeInstanceOf(SseStreamError)
    expect((error as Error).message).not.toContain('not json')
    expect(events).toEqual([{ type: 'token', text: 'one' }])
  })

  it('rejects a frame whose type is not in the endpoint contract', async () => {
    mockFetch(sseResponse([frame({ type: 'teleport' })]))
    await expect(
      streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, () => {}),
    ).rejects.toThrowError(SseStreamError)
  })

  it('rejects a token frame whose text is not a string', async () => {
    // The consumers append `text`; a non-string would have corrupted the answer.
    mockFetch(sseResponse([frame({ type: 'token', text: 7 })]))
    await expect(
      streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, () => {}),
    ).rejects.toThrowError(SseStreamError)
  })

  it('rejects a stream cut inside the final frame, keeping earlier events', async () => {
    mockFetch(
      sseResponse([frame({ type: 'start', message_id: 11 }), 'data: {"type":"token","text":"Hel']),
    )

    const events: ChatEvent[] = []
    const pending = streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, (e) =>
      events.push(e),
    )
    const error = await pending.catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(SseStreamError)
    expect(events).toEqual([{ type: 'start', message_id: 11 }])
  })

  it('rejects when the stream ends without a terminal frame', async () => {
    // Every turn route ends with `done` or an in-band `error`; EOF alone never
    // certifies completion, so a stream that stops short fails honestly.
    mockFetch(
      sseResponse([
        frame({ type: 'start', message_id: 11 }),
        frame({ type: 'token', text: 'Partial' }),
      ]),
    )

    const events: ChatEvent[] = []
    const pending = streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, (e) =>
      events.push(e),
    )
    const error = await pending.catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).message).toBe('The answer stopped early. Try again.')
    expect(events).toEqual([
      { type: 'start', message_id: 11 },
      { type: 'token', text: 'Partial' },
    ])
  })

  it('surfaces a throwing consumer and stops delivery', async () => {
    const frames = [
      frame({ type: 'start', message_id: 11 }),
      frame({ type: 'token', text: 'a' }),
      frame({ type: 'done', message_id: 11 }),
    ]
    let cancelled = 0
    mockFetch(sseResponse(frames, { onCancel: () => cancelled++ }))

    const events: ChatEvent[] = []
    const pending = streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, (e) => {
      events.push(e)
      throw new Error('consumer failed')
    })
    await expect(pending).rejects.toThrowError('consumer failed')
    // The buffered `done` after the failure can never convert it into success.
    expect(events).toEqual([{ type: 'start', message_id: 11 }])
    expect(cancelled).toBeGreaterThanOrEqual(1)
  })

  it('propagates an abort from the body stream', async () => {
    const encoder = new TextEncoder()
    mockFetch(
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode(frame({ type: 'token', text: 'a' })))
          },
          pull(controller) {
            controller.error(new DOMException('The operation was aborted.', 'AbortError'))
          },
        }),
      ),
    )

    await expect(
      streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, () => {}),
    ).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('raises the backend error rather than opening a reader', async () => {
    mockFetch(jsonResponse({ detail: 'No endpoint configured.' }, 400))
    await expect(
      streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, () => {}),
    ).rejects.toThrowError('No endpoint configured.')
  })
})

describe('byte-boundary replay (PLA-502)', () => {
  // Arbitrary byte boundaries: one chunk, byte-by-byte, and fixed odd splits that
  // land inside multibyte UTF-8, JSON escapes, and LaTeX delimiters.
  const PATTERNS = [[-1], [1], [2, 3, 5], [3, 1, 4], [7, 13, 2]]

  it('replays a tutor stream identically at every byte boundary', async () => {
    const events: ChatEvent[] = [
      { type: 'start', message_id: 11 },
      { type: 'status', stage: 'composing_answer' },
      { type: 'reasoning', text: 'thinking: € 𝄞 汉 🎉' },
      { type: 'token', text: 'So $\\frac{\\partial f}{\\partial x} = \\frac{1}{2}$' },
      { type: 'token', text: 'done: "quoted" \t end' },
      { type: 'done', message_id: 11 },
    ]
    const bytes = new TextEncoder().encode(
      events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join(''),
    )

    for (const pattern of PATTERNS) {
      mockFetch(sseBytesResponse(inChunks(bytes, pattern)))
      const seen: ChatEvent[] = []
      await streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, (e) => seen.push(e))
      expect(seen, `pattern ${JSON.stringify(pattern)}`).toEqual(events)
    }
  })

  it('replays the same stream framed with CRLF', async () => {
    const events: ChatEvent[] = [
      { type: 'token', text: '€ 𝄞 汉 🎉' },
      { type: 'done', message_id: 1 },
    ]
    const bytes = new TextEncoder().encode(
      events.map((e) => `data: ${JSON.stringify(e)}\r\n\r\n`).join(''),
    )

    mockFetch(sseBytesResponse(inChunks(bytes, [3, 5, 2, 1])))
    const seen: ChatEvent[] = []
    await streamChat(1, { content: 'hi', mode: 'guide', document_id: null }, (e) => seen.push(e))
    expect(seen).toEqual(events)
  })

  it('replays a /write stream over arbitrary byte boundaries', async () => {
    const events: WriteEvent[] = [
      { type: 'token', text: 'By $\\frac{d}{dx}$ of ' },
      { type: 'token', text: 'the energy, € falls.' },
      { type: 'done' },
    ]
    const bytes = new TextEncoder().encode(
      events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join(''),
    )

    for (const pattern of PATTERNS) {
      mockFetch(sseBytesResponse(inChunks(bytes, pattern)))
      const seen: WriteEvent[] = []
      await streamWrite(4, { instruction: 'expand' }, (e) => seen.push(e))
      expect(seen, `pattern ${JSON.stringify(pattern)}`).toEqual(events)
    }
  })

  it('replays a writer chat turn with its extra frames', async () => {
    const events: ChatEvent[] = [
      { type: 'start', message_id: 21 },
      { type: 'activity', tool: 'search', label: 'Searching notes', ok: true },
      { type: 'token', text: 'Draft: €' },
      { type: 'proposed', edit_id: 5 },
      { type: 'brief' },
      { type: 'done', message_id: 21 },
    ]
    const bytes = new TextEncoder().encode(
      events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join(''),
    )

    for (const pattern of PATTERNS) {
      mockFetch(sseBytesResponse(inChunks(bytes, pattern)))
      const seen: ChatEvent[] = []
      await streamWriterChat(4, 9, { content: 'write' }, (e) => seen.push(e))
      expect(seen, `pattern ${JSON.stringify(pattern)}`).toEqual(events)
    }
  })
})

describe('regenerate', () => {
  it('posts to the regenerate route and carries no question', async () => {
    // Retry means answer again, not ask again: the question is already stored.
    const spy = mockFetch(sseResponse(['data: {"type":"done"}\n\n']))
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
