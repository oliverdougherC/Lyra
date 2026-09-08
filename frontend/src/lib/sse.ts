/**
 * Shared SSE decoding and framing for Lyra's own streaming endpoints. This layer owns
 * bytes-to-events only; each endpoint's event and terminal semantics stay in
 * `lib/api.ts`. Full protocol rationale ships in the docs handoff (PLA-502).
 *
 * Policy (SSE rules, condensed):
 * - UTF-8 decodes incrementally with `fatal: true` (a broken byte sequence rejects the
 *   stream instead of being silently replaced), and the decoder is finalized at EOF, so
 *   a multibyte character split across chunks decodes to the same text as one that
 *   arrived whole.
 * - Lines end at U+000A; a preceding U+000D is stripped, so LF and CRLF frame alike.
 *   Events separate on blank lines.
 * - A `data` value strips at most one leading space after the colon; several `data:`
 *   lines in one event join with U+000A (standard multi-line-data policy - Lyra always
 *   sends one JSON object per event, so a non-JSON join is a malformed frame for the
 *   caller to reject, never two events).
 * - Comments (`:`), `event`/`id`/`retry`, unknown fields, and empty-data events are
 *   benign and discarded, never errors.
 * - Residual at EOF: a data line missing only its newline is delivered when its value
 *   is a complete JSON object (the lenient final-frame case the legacy readers
 *   consumed) and rejected otherwise - a stream cut inside a frame. A cut
 *   comment/metadata line is discarded. EOF alone never certifies completion:
 *   terminals are enforced by the caller.
 * - `onFrame` runs in stream order. A throw from it halts consumption, and no later
 *   buffered frame (including a `done`) can convert the failure into success. An
 *   `AbortSignal` checked around every delivered frame aborts with a `DOMException`
 *   even when more frames are already buffered.
 * - The reader is cancelled and its lock released in `finally` on every path (success,
 *   abort, error), so the transport cannot outlive the call. Generation isolation is
 *   owned by the UI, which guards its callbacks.
 */

/** A bounded, user-safe failure of the SSE transport itself (no payload is echoed). */
export class SseStreamError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'SseStreamError'
  }
}

/** A stream cut inside a data frame. */
export const STREAM_CUT_MESSAGE = 'The reply stopped before it finished. Try again.'

/** A byte sequence that is not valid UTF-8. */
export const STREAM_CORRUPT_MESSAGE = 'The reply was damaged in transit. Try again.'

/** One framed SSE event with a non-empty `data` field. */
export interface SseFrame {
  /** The `data` field, multi-line values joined with U+000A. */
  data: string
}

export type SseFrameCallback = (frame: SseFrame) => void

function isCompleteJsonObject(value: string): boolean {
  try {
    const parsed = JSON.parse(value)
    return parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)
  } catch {
    return false
  }
}

/**
 * Consume an SSE body stream, calling `onFrame` for each event in stream order.
 *
 * Rejects with the caller's callback error, an `SseStreamError` for a cut or corrupt
 * stream, the abort's `DOMException`, or the underlying read error. The reader is
 * always cancelled and released.
 */
export async function consumeSseStream(
  stream: ReadableStream<Uint8Array>,
  onFrame: SseFrameCallback,
  signal?: AbortSignal,
): Promise<void> {
  const reader = stream.getReader()
  const decoder = new TextDecoder('utf-8', { fatal: true })
  const decode = (chunk: Uint8Array, final: boolean): string => {
    try {
      return decoder.decode(chunk, { stream: !final })
    } catch {
      // Corrupt bytes are a framing failure, not a frame to skip: reject with a
      // safe message instead of a raw decoder exception.
      throw new SseStreamError(STREAM_CORRUPT_MESSAGE)
    }
  }
  const assertNotAborted = () => {
    if (signal?.aborted) {
      throw new DOMException('The operation was aborted.', 'AbortError')
    }
  }

  let pending = ''
  let dataLines: string[] = []

  // A bare `data:` line (empty event) is a keepalive no-op, not a frame.
  const deliver = (data: string) => {
    onFrame({ data })
    // An abort landing mid-chunk must stop delivery before the next buffered frame.
    assertNotAborted()
  }

  const flushEvent = () => {
    if (dataLines.length > 0) {
      const data = dataLines.join('\n')
      if (data.trim() !== '') deliver(data)
    }
    dataLines = []
  }

  const consumeLine = (raw: string) => {
    const line = raw.endsWith('\r') ? raw.slice(0, raw.length - 1) : raw
    if (line === '') {
      flushEvent()
      return
    }
    if (line.startsWith(':')) return // comment / keepalive
    const colon = line.indexOf(':')
    if (colon === -1) return // not a field
    const field = line.slice(0, colon)
    let value = line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'data') dataLines.push(value)
    // `event`, `id`, `retry`, and unknown fields are metadata: ignored.
  }

  try {
    for (;;) {
      assertNotAborted()
      const { done, value } = await reader.read()
      // An abort may land while the read is in flight: before any frame from the
      // chunk that just arrived is dispatched, stop - a cancelled turn must not
      // deliver what its transport is about to lose.
      assertNotAborted()
      if (done) {
        // Finalize the decoder: flush a multibyte sequence cut at EOF. The flushed
        // text cannot contain a newline (0x0A never appears inside a multibyte
        // sequence), so it can only extend the trailing partial line.
        pending += decode(new Uint8Array(), true)
        if (pending === 'data' || pending.startsWith('data:')) {
          const value = pending.slice(pending.indexOf(':') + 1)
          const data = value.startsWith(' ') ? value.slice(1) : value
          if (!isCompleteJsonObject(data)) {
            // The stream ended inside a data frame: the payload is unfinished, and
            // delivering what was received would invent a truncation as an event.
            throw new SseStreamError(STREAM_CUT_MESSAGE)
          }
          // A complete payload missing only its newline: the legacy readers consumed
          // it, so deliver it rather than losing a lenient server's final frame.
          dataLines.push(data)
        }
        // Any other residual (cut comment/metadata line, whitespace) carries no
        // payload: discard.
        pending = ''
        flushEvent()
        break
      }
      pending += decode(value, false)
      let newline = pending.indexOf('\n')
      while (newline !== -1) {
        consumeLine(pending.slice(0, newline))
        pending = pending.slice(newline + 1)
        newline = pending.indexOf('\n')
      }
    }
  } finally {
    // Cancel releases the body (and any in-flight read) on success, abort, callback
    // failure, and framing error alike; the lock frees the stream.
    await reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }
}
