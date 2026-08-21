import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createSaveEngine,
  flushOnHidden,
  SAVE_DEBOUNCE_MS,
  type SaveConflict,
  type SaveStateName,
  type WriteOutcome,
} from '@/lib/drafts/save-engine'

/**
 * A stale-version rejection the way `api.ts` raises it, and the classifier the workspace
 * passes to the engine to recognize it.
 */
class TestConflict extends Error {
  constructor(
    readonly serverVersion: number,
    readonly serverBody: string,
  ) {
    super('stale body version')
  }
}

const isConflict = (error: unknown): SaveConflict | null =>
  error instanceof TestConflict
    ? { serverVersion: error.serverVersion, serverBody: error.serverBody }
    : null

/**
 * A compare-and-swap draft body, exactly the server's contract, with writes held in a
 * queue so a test settles them in whatever order it wants. This is what lets us drive the
 * A-before-B and B-before-A interleavings deterministically.
 */
class FakeServer {
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

  /** How many writes are in flight against the server right now. */
  inFlight(): number {
    return this.queue.length
  }

  /** Apply the oldest queued write under the CAS rule and answer it. */
  async settleNext(): Promise<void> {
    const job = this.queue.shift()
    if (!job) throw new Error('no write in flight to settle')
    if (job.expected !== this.version) {
      job.reject(new TestConflict(this.version, this.body))
    } else {
      this.body = job.content
      this.version += 1
      job.resolve({ version: this.version })
    }
    await flushMicrotasks()
  }

  /** Fail the oldest queued write with an ordinary (non-conflict) error. */
  async failNext(message = 'Could not reach the Lyra server.'): Promise<void> {
    const job = this.queue.shift()
    if (!job) throw new Error('no write in flight to fail')
    job.reject(new Error(message))
    await flushMicrotasks()
  }

  async settleAll(): Promise<void> {
    while (this.queue.length) await this.settleNext()
  }
}

/** Let the engine's awaited continuations run without advancing any timer. */
async function flushMicrotasks(): Promise<void> {
  for (let i = 0; i < 5; i += 1) await Promise.resolve()
}

function makeEngine(server: FakeServer) {
  const states: Array<{ state: SaveStateName; detail?: string }> = []
  const engine = createSaveEngine({
    write: server.write,
    onState: (state, detail) => states.push({ state, detail }),
    isConflict,
  })
  engine.noteSaved(server.body, server.version)
  return { engine, states }
}

const names = (states: Array<{ state: SaveStateName }>): SaveStateName[] =>
  states.map((entry) => entry.state)

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('createSaveEngine: debounce and basic state', () => {
  it('writes once, 1.5s after the last change, with the version it last saw', async () => {
    const server = new FakeServer()
    const { engine, states } = makeEngine(server)

    engine.schedule('first')
    engine.schedule('second')

    // The timer started over: nothing has been written yet, and the state is dirty.
    expect(names(states)).toEqual(['dirty'])
    expect(server.inFlight()).toBe(0)

    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    expect(server.inFlight()).toBe(1)
    await server.settleNext()

    // One write, with the latest content and the seeded version, then saved.
    expect(server.body).toBe('second')
    expect(server.version).toBe(1)
    expect(engine.version()).toBe(1)
    expect(names(states)).toEqual(['dirty', 'saving', 'saved'])
  })

  it('does nothing when the content has not changed since the last save', async () => {
    const server = new FakeServer('already there', 3)
    const { engine, states } = makeEngine(server)

    engine.schedule('already there')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS * 2)

    expect(server.inFlight()).toBe(0)
    expect(names(states)).toEqual([])
    expect(engine.isDirty('already there')).toBe(false)
  })

  it('reports an error, stays dirty and retryable, then a retry reaches saved', async () => {
    const server = new FakeServer()
    const { engine, states } = makeEngine(server)

    engine.schedule('unsaved work')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    await server.failNext()

    expect(names(states)).toEqual(['dirty', 'saving', 'error'])
    expect(engine.isDirty('unsaved work')).toBe(true)

    // The engine re-armed the debounce after the failure; the retry now succeeds.
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    expect(server.inFlight()).toBe(1)
    await server.settleNext()

    expect(server.body).toBe('unsaved work')
    expect(names(states).at(-1)).toBe('saved')
    expect(engine.isDirty('unsaved work')).toBe(false)
  })
})

describe('createSaveEngine: ordering and coalescing', () => {
  it('never runs two writes at once and threads the version A -> B', async () => {
    const server = new FakeServer()
    const { engine } = makeEngine(server)
    let maxInFlight = 0
    const track = () => {
      maxInFlight = Math.max(maxInFlight, server.inFlight())
    }

    engine.schedule('a')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    track()
    // A is in flight. The student keeps typing; b then c arrive and coalesce.
    engine.schedule('b')
    engine.schedule('c')
    track()
    expect(server.inFlight()).toBe(1)

    await server.settleNext() // A lands: body 'a', version 1
    track()
    expect(server.body).toBe('a')

    // The coalesced newest ('c') is written next, on the version A produced.
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    track()
    expect(server.inFlight()).toBe(1)
    await server.settleNext()

    expect(maxInFlight).toBe(1)
    expect(server.body).toBe('c') // 'b' was coalesced away, never written
    expect(server.version).toBe(2)
    expect(engine.lastSaved()).toBe('c')
    expect(engine.isDirty('c')).toBe(false)
  })

  it('an older write can never land after a newer one from the same editor', async () => {
    // The core of the bug: the engine only ever creates write B after write A has settled,
    // so their responses cannot arrive out of order.
    const server = new FakeServer()
    const { engine } = makeEngine(server)

    engine.schedule('old')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    engine.schedule('new')
    expect(server.inFlight()).toBe(1) // only 'old' is in flight; 'new' is pending, not sent

    await server.settleNext() // 'old' lands first, by construction
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    expect(server.inFlight()).toBe(1) // now 'new' is sent, after 'old' resolved
    await server.settleNext()

    expect(server.body).toBe('new')
    expect(server.version).toBe(2)
  })
})

describe('createSaveEngine: flush joins the pipeline', () => {
  it('flushes immediately and drops the scheduled write, so nothing is written twice', async () => {
    const server = new FakeServer()
    const { engine } = makeEngine(server)

    engine.schedule('pending change')
    expect(engine.pending()).toBe(true)

    const flushed = engine.flush('pending change')
    expect(server.inFlight()).toBe(1)
    await server.settleNext()
    await flushed
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS * 2)

    expect(server.body).toBe('pending change')
    expect(server.version).toBe(1)
    expect(engine.pending()).toBe(false)
  })

  it('a flush during an active save joins it rather than starting a competing request', async () => {
    const server = new FakeServer()
    const { engine } = makeEngine(server)

    engine.schedule('a')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    expect(server.inFlight()).toBe(1) // A owns the pipeline

    // Flush the same content while A is in flight: no second request is created.
    const flushed = engine.flush('a')
    expect(server.inFlight()).toBe(1)

    await server.settleNext()
    await flushed

    expect(server.body).toBe('a')
    expect(server.version).toBe(1)
    expect(server.inFlight()).toBe(0)
  })

  it('a flush during an active save writes newer typed content next, in order', async () => {
    const server = new FakeServer()
    const { engine } = makeEngine(server)

    engine.schedule('a')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    // A in flight; the student types 'b', then a flush (tab hide) asks for the newest.
    engine.schedule('b')
    const flushed = engine.flush('b')
    expect(server.inFlight()).toBe(1) // still only A

    await server.settleNext() // A lands
    await flushMicrotasks()
    expect(server.inFlight()).toBe(1) // flush drove B without waiting for the debounce
    await server.settleNext() // B lands
    await flushed

    expect(server.body).toBe('b')
    expect(server.version).toBe(2)
  })

  it('treats a flush with nothing open as a no-op but still awaits an active write', async () => {
    const server = new FakeServer()
    const { engine } = makeEngine(server)

    await engine.flush(null)
    expect(server.inFlight()).toBe(0)

    engine.schedule('a')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    const flushed = engine.flush(null) // nothing new, but A is in flight
    await server.settleNext()
    await flushed
    expect(server.body).toBe('a')
  })

  it('flushes the newest content when the tab goes hidden, mid-save', async () => {
    const server = new FakeServer()
    const { engine } = makeEngine(server)
    let visibility: DocumentVisibilityState = 'visible'
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => visibility,
    })
    const detach = flushOnHidden(() => void engine.flush('hidden-body'))
    try {
      engine.schedule('typed')
      await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
      expect(server.inFlight()).toBe(1) // 'typed' in flight

      visibility = 'hidden'
      document.dispatchEvent(new Event('visibilitychange')) // flush('hidden-body')
      expect(server.inFlight()).toBe(1) // no competing request while 'typed' is in flight

      await server.settleAll()
      await flushMicrotasks()
      await server.settleAll()

      expect(server.body).toBe('hidden-body')
    } finally {
      detach()
      Reflect.deleteProperty(document, 'visibilityState')
    }
  })
})

describe('createSaveEngine: stale-version conflict', () => {
  it('a stale write goes to conflict, keeps local text, and never reports saved', async () => {
    // The server has already moved past what this editor knows: version 2 holds newer text,
    // but the editor last read version 1.
    const server = new FakeServer('newer text from elsewhere', 2)
    const states: SaveStateName[] = []
    const engine = createSaveEngine({
      write: server.write,
      onState: (state) => states.push(state),
      isConflict,
    })
    engine.noteSaved('what the editor last read', 1)

    engine.schedule('my local edit')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    await server.settleNext() // expected 1, server is 2 -> conflict

    expect(engine.conflict()).toEqual({ serverVersion: 2, serverBody: 'newer text from elsewhere' })
    expect(engine.isDirty('my local edit')).toBe(true)
    expect(states).not.toContain('saved')
    expect(states.at(-1)).toBe('conflict')
    // The server was not overwritten by the stale write.
    expect(server.body).toBe('newer text from elsewhere')
  })

  it('keepLocal rebases onto the server version and writes the local text over it', async () => {
    const server = new FakeServer('theirs', 2)
    const states: SaveStateName[] = []
    const engine = createSaveEngine({
      write: server.write,
      onState: (state) => states.push(state),
      isConflict,
    })
    engine.noteSaved('base the editor read', 1)

    engine.schedule('mine')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    await server.settleNext() // conflict at version 2
    expect(engine.conflict()).not.toBeNull()

    engine.keepLocal('mine')
    expect(server.inFlight()).toBe(1) // deliberate write, issued at once
    await server.settleNext()

    expect(server.body).toBe('mine') // local text won
    expect(server.version).toBe(3)
    expect(engine.conflict()).toBeNull()
    expect(states.at(-1)).toBe('saved')
  })

  it('takeServer adopts the server text and version and returns it for the editor', async () => {
    const server = new FakeServer('theirs', 2)
    const states: SaveStateName[] = []
    const engine = createSaveEngine({
      write: server.write,
      onState: (state) => states.push(state),
      isConflict,
    })
    engine.noteSaved('base the editor read', 1)

    engine.schedule('mine')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    await server.settleNext() // conflict

    const adopted = engine.takeServer()
    expect(adopted).toEqual({ serverVersion: 2, serverBody: 'theirs' })
    expect(engine.conflict()).toBeNull()
    expect(engine.lastSaved()).toBe('theirs')
    expect(engine.version()).toBe(2)
    expect(states.at(-1)).toBe('saved')
    // A later edit saves cleanly against the adopted version.
    engine.schedule('theirs, extended')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    await server.settleNext()
    expect(server.body).toBe('theirs, extended')
    expect(server.version).toBe(3)
  })
})

describe('createSaveEngine: two editors against one body (stale tab)', () => {
  it('the second, stale tab conflicts instead of overwriting the first tab', async () => {
    const server = new FakeServer('shared start', 0)
    const one = makeEngine(server)
    const two = makeEngine(server) // both seeded at version 0

    // Tab one saves and wins, moving the body to version 1.
    one.engine.schedule('one edits')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    await server.settleNext()
    expect(server.body).toBe('one edits')
    expect(server.version).toBe(1)

    // Tab two, still at version 0, tries to save its own text: refused, not applied.
    two.engine.schedule('two edits, from the stale tab')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    await server.settleNext()

    expect(server.body).toBe('one edits') // tab one's work survived
    expect(two.engine.conflict()).toEqual({ serverVersion: 1, serverBody: 'one edits' })
    expect(names(two.states)).not.toContain('saved')
    // Tab one never learns of a regression; its own state stayed saved.
    expect(names(one.states).at(-1)).toBe('saved')
  })
})

describe('flushOnHidden', () => {
  it('flushes when the tab goes hidden, and only then', () => {
    const flush = vi.fn()
    const detach = flushOnHidden(flush)

    let visibility: DocumentVisibilityState = 'visible'
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => visibility,
    })
    try {
      document.dispatchEvent(new Event('visibilitychange'))
      expect(flush).not.toHaveBeenCalled()

      visibility = 'hidden'
      document.dispatchEvent(new Event('visibilitychange'))
      expect(flush).toHaveBeenCalledTimes(1)

      detach()
      document.dispatchEvent(new Event('visibilitychange'))
      expect(flush).toHaveBeenCalledTimes(1)
    } finally {
      Reflect.deleteProperty(document, 'visibilityState')
    }
  })
})
