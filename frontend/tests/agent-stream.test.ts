import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, type AgentStreamEvent } from '@/lib/api'
import { SseStreamError } from '@/lib/sse'

const result = { message_id: 4, content: 'Hello world', stopped: 'complete', activity: [] }
const frame = (value: unknown) => `data: ${JSON.stringify(value)}\n\n`
function start() {
  let controller!: ReadableStreamDefaultController<Uint8Array>
  const response = new Response(
    new ReadableStream<Uint8Array>({
      start(c) {
        controller = c
      },
    }),
    {
      headers: { 'content-type': 'text/event-stream' },
    },
  )
  const fetch = vi.fn().mockResolvedValue(response)
  vi.stubGlobal('fetch', fetch)
  return {
    fetch,
    push: (text: string) => controller.enqueue(new TextEncoder().encode(text)),
    close: () => controller.close(),
  }
}
beforeEach(() => {
  vi.unstubAllGlobals()
  window.__LYRA_BOOTSTRAP__ = { apiBase: 'http://127.0.0.1:8000' }
})
describe('agent response streaming', () => {
  it('delivers split reasoning and answer frames before the final result', async () => {
    const stream = start()
    const events: AgentStreamEvent[] = []
    const pending = api.sendAgentChat(
      1,
      7,
      'Question',
      undefined,
      null,
      'guide',
      'operation',
      undefined,
      (e) => events.push(e),
    )
    let finished = false
    void pending.then(() => {
      finished = true
    })
    stream.push('data: {"type":"reason')
    stream.push('ing","text":"Working"}\n\n' + frame({ type: 'token', text: 'Hello' }))
    await vi.waitFor(() => expect(events).toHaveLength(2))
    expect(finished).toBe(false)
    expect(stream.fetch.mock.calls[0][1].headers.accept).toBe('text/event-stream')
    stream.push(
      frame({ type: 'reset' }) +
        frame({ type: 'token', text: 'Hello world' }) +
        frame({ type: 'result', result }),
    )
    stream.close()
    expect(await pending).toEqual(result)
    expect(events.map((e) => e.type)).toEqual(['reasoning', 'token', 'reset', 'token'])
  })
  it('preserves structured conflict codes from errors after stream headers', async () => {
    const stream = start()
    const pending = api.retryAgentChat(1, 7, undefined, undefined, () => {})
    stream.push(
      frame({ type: 'error', status: 409, detail: 'Mismatch', code: 'operation_id_mismatch' }),
    )
    stream.close()
    await expect(pending).rejects.toMatchObject({ status: 409, code: 'operation_id_mismatch' })
  })
  it('rejects a truncated transport instead of accepting its partial answer', async () => {
    const stream = start()
    const pending = api.regenerateAgentChat(1, 7, undefined, undefined, () => {})
    stream.push(frame({ type: 'token', text: 'Partial' }))
    stream.close()
    await expect(pending).rejects.toBeInstanceOf(ApiError)
  })
  it('accepts a JSON replay from a server without streaming', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify(result), { headers: { 'content-type': 'application/json' } }),
        ),
    )
    expect(await api.retryAgentChat(1, 7, undefined, undefined, () => {})).toEqual(result)
  })

  it('rejects an unknown frame type instead of skipping it', async () => {
    const stream = start()
    const pending = api.sendAgentChat(
      1,
      7,
      'Question',
      undefined,
      null,
      'guide',
      'operation',
      undefined,
      () => {},
    )
    stream.push(frame({ type: 'token', text: 'Hello' }))
    stream.push(frame({ type: 'teleport' }))
    stream.close()
    await expect(pending).rejects.toThrowError(SseStreamError)
  })

  it('rejects a token frame without a string text', async () => {
    const stream = start()
    const pending = api.sendAgentChat(
      1,
      7,
      'Question',
      undefined,
      null,
      'guide',
      'operation',
      undefined,
      () => {},
    )
    stream.push(frame({ type: 'token' }))
    stream.close()
    await expect(pending).rejects.toThrowError(SseStreamError)
  })

  it('rejects when the stream is cut inside the final frame', async () => {
    const stream = start()
    const pending = api.sendAgentChat(
      1,
      7,
      'Question',
      undefined,
      null,
      'guide',
      'operation',
      undefined,
      () => {},
    )
    stream.push(frame({ type: 'token', text: 'Hello' }))
    stream.push('data: {"type":"res')
    stream.close()
    const error = await pending.catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(SseStreamError)
    // Bounded message: no raw payload text leaks into the failure.
    expect((error as Error).message).not.toContain('res')
  })

  it('rejects a result that lacks the fields the UI reads', async () => {
    // A `result: {}` must not certify success: the turn's outcome is unknown.
    const stream = start()
    const pending = api.sendAgentChat(
      1,
      7,
      'Question',
      undefined,
      null,
      'guide',
      'operation',
      undefined,
      () => {},
    )
    stream.push(frame({ type: 'result', result: {} }))
    stream.close()
    await expect(pending).rejects.toThrowError(SseStreamError)
  })

  it('accepts a final result missing only its newline', async () => {
    // The legacy reader consumed a complete final payload whose terminator was never
    // flushed; that leniency is preserved (only a cut payload fails).
    const stream = start()
    const pending = api.sendAgentChat(
      1,
      7,
      'Question',
      undefined,
      null,
      'guide',
      'operation',
      undefined,
      () => {},
    )
    stream.push(frame({ type: 'token', text: 'Hello world' }))
    stream.push(`data: ${JSON.stringify({ type: 'result', result })}`)
    stream.close()
    expect(await pending).toEqual(result)
  })

  it('surfaces a throwing callback without consuming later frames', async () => {
    const stream = start()
    const events: AgentStreamEvent[] = []
    let calls = 0
    const pending = api.sendAgentChat(
      1,
      7,
      'Question',
      undefined,
      null,
      'guide',
      'operation',
      undefined,
      (e) => {
        events.push(e)
        calls++
        if (calls === 1) throw new Error('consumer failed')
      },
    )
    stream.push(frame({ type: 'token', text: 'Hello' }))
    stream.push(frame({ type: 'result', result })) // buffered; must never be consumed
    stream.close()
    await expect(pending).rejects.toThrowError('consumer failed')
    expect(events.map((e) => e.type)).toEqual(['token'])
  })
})
