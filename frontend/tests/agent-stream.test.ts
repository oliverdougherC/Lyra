import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, type AgentStreamEvent } from '@/lib/api'

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
})
