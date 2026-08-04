import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api, streamChat, streamRegenerate } from '@/lib/api'
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
