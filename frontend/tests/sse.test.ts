import { describe, expect, it } from 'vitest'

import {
  consumeSseStream,
  type SseFrame,
  SseStreamError,
  STREAM_CORRUPT_MESSAGE,
  STREAM_CUT_MESSAGE,
} from '@/lib/sse'

const encoder = new TextEncoder()

/**
 * A ReadableStream with a retained controller, so a test pushes caller-chosen byte
 * runs (arbitrary chunk boundaries) and observes cancel/release on the underlying
 * stream.
 */
function harness() {
  let controller!: ReadableStreamDefaultController<Uint8Array>
  let cancelCalls = 0
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c
    },
    cancel: () => {
      cancelCalls++
    },
  })
  return {
    stream,
    push: (text: string) => controller.enqueue(encoder.encode(text)),
    pushBytes: (bytes: Uint8Array) => controller.enqueue(bytes),
    error: (err: unknown) => controller.error(err),
    close: () => controller.close(),
    cancelCalls: () => cancelCalls,
    /** True when the consumer released its lock (a second reader can be taken). */
    lockFree: () => {
      try {
        stream.getReader()
        return true
      } catch {
        return false
      }
    },
  }
}

/** Feed `bytes` in the given chunk sizes (-1 = one chunk), close, collect frames. */
async function replay(bytes: Uint8Array, sizes: number[], onFrame: (frame: SseFrame) => void) {
  const h = harness()
  const pending = consumeSseStream(h.stream, onFrame)
  let offset = 0
  if (sizes.length === 1 && sizes[0] === -1) {
    h.pushBytes(bytes)
  } else {
    for (const size of sizes) {
      h.pushBytes(bytes.subarray(offset, offset + size))
      offset += size
    }
    if (offset < bytes.length) h.pushBytes(bytes.subarray(offset))
  }
  h.close()
  await pending
  return h
}

/** A valid stream whose payload splits multibyte UTF-8, JSON escapes, and LaTeX delimiters. */
const FIXTURE_EVENTS = [
  { type: 'start', message_id: 11 },
  { type: 'reasoning', text: 'thinking: € 𝄞 汉 🎉' },
  { type: 'token', text: 'So $\\frac{\\partial f}{\\partial x} = \\frac{1}{2}$' },
  { type: 'token', text: 'done: "quoted" \t end' },
  { type: 'done', message_id: 11 },
]
const fixtureBytes = encoder.encode(
  FIXTURE_EVENTS.map((e) => `data: ${JSON.stringify(e)}\n\n`).join(''),
)
const EXPECTED_FRAMES = FIXTURE_EVENTS.map((e) => JSON.stringify(e))

describe('consumeSseStream', () => {
  it('replays a valid stream identically at every byte boundary', async () => {
    // -1 = whole payload in one chunk; the rest split multibyte characters,
    // escape sequences, and LaTeX delimiters mid-stream.
    const patterns = [[-1], [1], [2, 3, 5], [3, 1, 4], [7, 13, 2, 5, 1]]
    for (const pattern of patterns) {
      const frames: string[] = []
      const h = await replay(fixtureBytes, pattern, (f) => frames.push(f.data))
      expect(frames, `chunk pattern ${JSON.stringify(pattern)}`).toEqual(EXPECTED_FRAMES)
      // The source is already closed here, so `cancel` is a no-op; the consumer's
      // release is proven by the lock being free for a later reader.
      expect(h.lockFree()).toBe(true)
    }
  })

  it('frames CRLF streams identically to LF streams', async () => {
    const crlf = encoder.encode(
      'data: {"type":"token","text":"a"}\r\n\r\ndata: {"type":"done"}\r\n\r\n',
    )
    const frames: string[] = []
    await replay(crlf, [5, 11, 7], (f) => frames.push(f.data))
    expect(frames).toEqual(['{"type":"token","text":"a"}', '{"type":"done"}'])
  })

  it('ignores comments, metadata fields, and unknown fields', async () => {
    const h = harness()
    h.push(': keep')
    h.push('-alive\n') // a comment split across the boundary
    h.push('event: message\nid: 42\nretry: 3000\nnote: hello\n')
    h.push('data: {"type":"token","text":"a"}\n\n')
    h.close()
    const frames: string[] = []
    await consumeSseStream(h.stream, (f) => frames.push(f.data))
    expect(frames).toEqual(['{"type":"token","text":"a"}'])
    expect(h.lockFree()).toBe(true)
  })

  it('joins multi-line data with a newline (the documented policy)', async () => {
    const h = harness()
    h.push('data: a\ndata: b\n\n')
    h.close()
    const frames: string[] = []
    await consumeSseStream(h.stream, (f) => frames.push(f.data))
    // One event, its two data lines joined - Lyra's one-JSON-object-per-event
    // contract makes a non-JSON join a malformed frame for the caller.
    expect(frames).toEqual(['a\nb'])
  })

  it('skips empty-data events as keepalives', async () => {
    const h = harness()
    h.push('data:\n\n')
    h.push('data: \n\n')
    h.push('data: {"type":"done"}\n\n')
    h.close()
    const frames: string[] = []
    await consumeSseStream(h.stream, (f) => frames.push(f.data))
    expect(frames).toEqual(['{"type":"done"}'])
  })

  it('delivers a final event missing only its blank-line terminator', async () => {
    const h = harness()
    h.push('data: {"type":"token","text":"a"}\n\n')
    h.push('data: {"type":"done"}') // complete JSON, no trailing newline
    h.close()
    const frames: string[] = []
    await consumeSseStream(h.stream, (f) => frames.push(f.data))
    expect(frames).toEqual(['{"type":"token","text":"a"}', '{"type":"done"}'])
  })

  it('rejects a stream cut inside a data frame with a bounded message', async () => {
    const h = harness()
    h.push('data: {"type":"token","text":"a"}\n\n')
    h.push('data: {"type":"token","text":"Hel') // cut mid-JSON, no newline
    h.close()
    const seen: string[] = []
    const pending = consumeSseStream(h.stream, (f) => seen.push(f.data))
    await expect(pending).rejects.toThrowError(SseStreamError)
    await expect(pending).rejects.toThrowError(STREAM_CUT_MESSAGE)
    // The error carries no payload from the stream.
    expect(STREAM_CUT_MESSAGE).not.toContain('Hel')
    expect(seen).toEqual(['{"type":"token","text":"a"}'])
    expect(h.lockFree()).toBe(true)
  })

  it('rejects when the cut line could become a data line even after a complete frame', async () => {
    const h = harness()
    h.push('data: {"type":"token","text":"a"}\n\n')
    h.push('data: {"type":"token","text":"b"}\ndata: {"t') // next frame started, cut
    h.close()
    await expect(consumeSseStream(h.stream, () => {})).rejects.toThrowError(STREAM_CUT_MESSAGE)
  })

  it('discards a cut comment or metadata line as benign', async () => {
    for (const tail of [': keep', 'event: stat', '  ']) {
      const h = harness()
      h.push('data: {"type":"done"}\n')
      h.push(tail)
      h.close()
      const frames: string[] = []
      await consumeSseStream(h.stream, (f) => frames.push(f.data))
      expect(frames, `tail ${JSON.stringify(tail)}`).toEqual(['{"type":"done"}'])
    }
  })

  it('rejects bytes that are not valid UTF-8 instead of replacing them', async () => {
    const h = harness()
    h.pushBytes(new Uint8Array([0xff, 0xfe, 0xfd]))
    h.close()
    await expect(consumeSseStream(h.stream, () => {})).rejects.toThrowError(STREAM_CORRUPT_MESSAGE)
    expect(h.lockFree()).toBe(true)
  })

  it('rejects a multibyte sequence truncated at EOF', async () => {
    const h = harness()
    h.push('data: {"text":"')
    h.pushBytes(new Uint8Array([0xe2, 0x82])) // the start of a truncated €
    h.close()
    await expect(consumeSseStream(h.stream, () => {})).rejects.toThrowError(SseStreamError)
  })

  it('halts on a throwing callback and never delivers later buffered frames', async () => {
    const h = harness()
    h.push('data: one\n\n')
    h.push('data: two\n\n')
    h.push('data: done\n\n')
    h.close()
    const seen: string[] = []
    const pending = consumeSseStream(h.stream, (f) => {
      seen.push(f.data)
      throw new Error('consumer failed')
    })
    await expect(pending).rejects.toThrowError('consumer failed')
    expect(seen).toEqual(['one'])
    expect(h.cancelCalls()).toBeGreaterThanOrEqual(1)
    expect(h.lockFree()).toBe(true)
  })

  it('propagates a transport failure (abort) while frames are queued', async () => {
    const h = harness()
    h.push('data: one\n\n')
    h.error(new DOMException('The operation was aborted.', 'AbortError'))
    const seen: string[] = []
    const pending = consumeSseStream(h.stream, (f) => seen.push(f.data))
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    // An errored stream delivers no queued-but-unread frame; the reader is released.
    expect(seen).toEqual([])
    expect(h.lockFree()).toBe(true)
  })

  it('respects an AbortSignal for frames already buffered in the stream', async () => {
    const controller = new AbortController()
    const h = harness()
    h.push('data: one\n\n')
    h.push('data: two\n\n') // buffered, but the abort lands after frame one
    h.close()
    const seen: string[] = []
    const pending = consumeSseStream(
      h.stream,
      (f) => {
        seen.push(f.data)
        if (f.data === 'one') controller.abort()
      },
      controller.signal,
    )
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(seen).toEqual(['one'])
    expect(h.cancelCalls()).toBeGreaterThanOrEqual(1)
    expect(h.lockFree()).toBe(true)
  })

  it('rejects before reading when the signal is already aborted', async () => {
    const controller = new AbortController()
    controller.abort()
    const h = harness()
    h.push('data: one\n\n')
    h.close()
    const pending = consumeSseStream(h.stream, () => {}, controller.signal)
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('aborts before dispatching a frame queued behind an in-flight read', async () => {
    const controller = new AbortController()
    const h = harness()
    // Nothing is enqueued: the consumer's first read stays pending until data arrives.
    const seen: string[] = []
    const pending = consumeSseStream(h.stream, (f) => seen.push(f.data), controller.signal)
    // The abort lands while the read is in flight; the data that then satisfies it
    // must not be dispatched.
    controller.abort()
    h.push('data: one\n\n')
    h.push('data: two\n\n')
    h.close()
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(seen).toEqual([])
    expect(h.lockFree()).toBe(true)
  })
})
