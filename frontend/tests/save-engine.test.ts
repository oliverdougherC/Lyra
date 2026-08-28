import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createSaveEngine,
  decideServerSync,
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

describe('createSaveEngine: reverting while a different write is in flight', () => {
  it('a revert to the last-saved text mid-write still converges and never says saved while they differ', async () => {
    // The exact interleaving from the review: S is saved, the user types A, autosave A
    // starts, the user reverts to S before A resolves, then A resolves. The engine must
    // still owe a corrective write of S, converge editor and server on S, and never once
    // report saved while the editor shows S and the server holds A.
    const server = new FakeServer('S', 0)
    const { engine, states } = makeEngine(server)

    engine.schedule('A')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    expect(server.inFlight()).toBe(1) // A in flight at version 0

    // Undo back to S while A is still in the air.
    engine.schedule('S')

    await server.settleNext() // A lands: the server now holds 'A' at version 1
    expect(engine.lastSaved()).toBe('A')
    // A corrective write of S is owed; the state is dirty, emphatically not saved.
    expect(names(states).at(-1)).toBe('dirty')
    expect(names(states)).not.toContain('saved')
    expect(engine.isDirty('S')).toBe(true)

    // The corrective write of S goes out after A, in order, and converges the two.
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    expect(server.inFlight()).toBe(1)
    await server.settleNext()

    expect(server.body).toBe('S') // editor and server converge on S
    expect(server.version).toBe(2)
    expect(engine.lastSaved()).toBe('S')
    expect(engine.isDirty('S')).toBe(false)
    expect(names(states).at(-1)).toBe('saved')
    // Saved appeared exactly once, and only at the end when they finally matched.
    expect(names(states).filter((state) => state === 'saved')).toHaveLength(1)
  })

  it('typing then reverting to the body already in flight coalesces to nothing owed', async () => {
    const server = new FakeServer('start', 0)
    const { engine, states } = makeEngine(server)

    engine.schedule('A')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    expect(server.inFlight()).toBe(1) // A in flight

    engine.schedule('B') // newer text pends
    engine.schedule('A') // then reverted to exactly the body in flight
    expect(engine.pending()).toBe(false) // nothing more is owed; the timer was cleared

    await server.settleNext() // A lands
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)

    expect(server.inFlight()).toBe(0) // no corrective write; B was discarded
    expect(server.body).toBe('A')
    expect(server.version).toBe(1)
    expect(engine.isDirty('A')).toBe(false)
    expect(names(states).at(-1)).toBe('saved')
  })
})

describe('createSaveEngine: an authoritative server op raises a conflict', () => {
  it('forceConflict while an autosave is pending keeps the local text and reconciles', async () => {
    // A pass or an accepted suggestion rewrote the body to version 5 while the student had
    // unsaved local text pending in the debounce.
    const server = new FakeServer('the server rewrote this', 5)
    const { engine, states } = makeEngine(server)
    engine.noteSaved('what the editor read', 1)

    engine.schedule('local edit')
    expect(engine.pending()).toBe(true)

    engine.forceConflict('the server rewrote this', 5)
    expect(engine.conflict()).toEqual({ serverVersion: 5, serverBody: 'the server rewrote this' })
    expect(engine.isDirty('local edit')).toBe(true)
    expect(names(states).at(-1)).toBe('conflict')
    expect(names(states)).not.toContain('saved')

    // Keeping the local text rebases onto the server's version and writes it over.
    engine.keepLocal('local edit')
    await server.settleNext()
    expect(server.body).toBe('local edit')
    expect(server.version).toBe(6)
    expect(engine.conflict()).toBeNull()
    expect(names(states).at(-1)).toBe('saved')
  })

  it('forceConflict while a write is in flight never lets that write report saved', async () => {
    const server = new FakeServer('base', 3)
    const { engine, states } = makeEngine(server)

    engine.schedule('local edit')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    expect(server.inFlight()).toBe(1) // 'local edit' in flight, expects version 3

    // The server advances underneath the in-flight write (an authoritative op landed).
    server.version = 9
    server.body = 'rewritten elsewhere'
    engine.forceConflict('rewritten elsewhere', 9)
    expect(names(states).at(-1)).toBe('conflict')

    await server.settleNext() // the stale write is refused by the CAS too
    expect(engine.conflict()).toEqual({ serverVersion: 9, serverBody: 'rewritten elsewhere' })
    expect(engine.isDirty('local edit')).toBe(true)
    expect(names(states)).not.toContain('saved')
    expect(server.body).toBe('rewritten elsewhere') // the local write never landed
  })
})

describe('createSaveEngine: Keep-what-I-wrote adopts the server baseline, not stale lastSaved', () => {
  // The false-Saved bug (PLA-289 round 3): keepLocal decided whether a write was owed by
  // comparing the chosen text against the *pre-conflict* lastSaved. A conflict is proof that
  // value is no longer what the server holds, so when local == old lastSaved but != the
  // server body, keepLocal enqueued nothing, cleared the conflict, and reported Saved over a
  // body the server did not hold. The baseline must become the conflict's serverBody.

  function conflictedEngine(serverBody: string, serverVersion: number, seededBody: string) {
    // The engine believes the last saved body is `seededBody`, but the server actually holds
    // `serverBody@serverVersion` (another tab wrote it). A snapshot/op raised the conflict.
    const server = new FakeServer(serverBody, serverVersion)
    const states: SaveStateName[] = []
    const engine = createSaveEngine({
      write: server.write,
      onState: (state) => states.push(state),
      isConflict,
    })
    engine.noteSaved(seededBody, serverVersion - 1)
    engine.forceConflict(serverBody, serverVersion)
    return { server, engine, states }
  }

  it('writes local over the server when local equals old lastSaved but differs from the server body', async () => {
    // local S equals the pre-conflict lastSaved S, but the server holds X. Old code owed
    // nothing; the fix owes a write of S and withholds Saved until it lands.
    const { server, engine, states } = conflictedEngine('X', 5, 'S')

    engine.keepLocal('S')
    // A corrective write was actually issued, and Saved is not reported before it lands.
    expect(server.inFlight()).toBe(1)
    expect(engine.conflict()).toBeNull()
    expect(states.at(-1)).not.toBe('saved')

    await server.settleNext()
    expect(server.body).toBe('S') // the server now holds the student's text
    expect(server.version).toBe(6)
    expect(engine.lastSaved()).toBe('S')
    expect(states.at(-1)).toBe('saved')
  })

  it('models the snapshot path: a lost CAS at vN feeds forceConflict, Keep writes local over the other tab', async () => {
    // Local/server are S@4; another tab writes X@5; a snapshot CAS at v4 loses and raises the
    // conflict while the editor still shows S. Keep must land S on the server, not report a
    // false Saved.
    const { server, engine } = conflictedEngine('X', 5, 'S')
    expect(server.body).toBe('X')

    engine.keepLocal('S')
    await server.settleNext()

    expect(server.body).toBe('S')
    expect(server.version).toBe(6)
    expect(engine.conflict()).toBeNull()
  })

  it('adopts without a redundant write when the local text already equals the server body', async () => {
    const { server, engine, states } = conflictedEngine('X', 5, 'S')

    engine.keepLocal('X') // the student chose text that already matches the server
    expect(server.inFlight()).toBe(0) // no write: bytes already match
    expect(engine.conflict()).toBeNull()
    expect(engine.lastSaved()).toBe('X')
    expect(engine.version()).toBe(5)
    expect(states.at(-1)).toBe('saved')
  })

  it('S saved, A in flight, revert to S, another writer moves the body, A conflicts, Keep S lands S', async () => {
    const server = new FakeServer('S', 0)
    const { engine, states } = makeEngine(server)

    engine.schedule('A')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    expect(server.inFlight()).toBe(1) // A in flight, expects version 0
    engine.schedule('S') // revert to the saved text while A is still in the air

    // Another writer moves the server to X@1 before A's response arrives.
    server.body = 'X'
    server.version = 1
    await server.settleNext() // A is refused by the CAS -> conflict {1, 'X'}
    expect(engine.conflict()).toEqual({ serverVersion: 1, serverBody: 'X' })

    engine.keepLocal('S') // the editor shows S; keep it
    expect(server.inFlight()).toBe(1)
    await server.settleNext()

    expect(server.body).toBe('S')
    expect(server.version).toBe(2)
    expect(engine.conflict()).toBeNull()
    expect(names(states).at(-1)).toBe('saved')
  })

  it('a late stale response from a write in flight when the conflict was raised cannot resurrect it', async () => {
    // Robustness: forceConflict happens while an older autosave is still in flight, then the
    // student resolves the conflict, and only then does the old write fail with a stale 409.
    // That late response must be dropped, not allowed to reopen the resolved conflict.
    const server = new FakeServer('S', 0)
    const { engine, states } = makeEngine(server)

    engine.schedule('A')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    expect(server.inFlight()).toBe(1) // A in flight

    // The server moves to X@1 and the workspace raises the conflict (e.g. a lost snapshot CAS)
    // while A is still unresolved.
    server.body = 'X'
    server.version = 1
    engine.forceConflict('X', 1)

    // The student keeps their text; A is still in flight, so the corrective write is deferred.
    engine.keepLocal('A')
    expect(engine.conflict()).toBeNull()

    // Now the old A write finally fails with a stale 409. It must be ignored.
    await server.settleNext()
    expect(engine.conflict()).toBeNull() // not resurrected
    expect(engine.lastSaved()).not.toBe('A') // the dropped write did not confirm A

    // The deferred corrective write then lands A on top of the rebased baseline.
    await server.settleNext()
    expect(server.body).toBe('A')
    expect(server.version).toBe(2)
    expect(engine.conflict()).toBeNull()
    expect(names(states).at(-1)).toBe('saved')
  })
})

describe('createSaveEngine: flush proves the newest body is authoritative', () => {
  it('resolves ok with the confirmed version when the newest body is saved', async () => {
    const server = new FakeServer('', 0)
    const { engine } = makeEngine(server)

    engine.schedule('newest')
    const flushed = engine.flush('newest')
    await server.settleNext()

    await expect(flushed).resolves.toEqual({ ok: true, status: 'saved', version: 1 })
  })

  it('resolves an error result when the write fails, and stays dirty and retryable', async () => {
    const server = new FakeServer('', 0)
    const { engine } = makeEngine(server)

    engine.schedule('newest')
    const flushed = engine.flush('newest')
    await server.failNext('offline')
    const result = await flushed

    expect(result.ok).toBe(false)
    expect(result.status).toBe('error')
    expect(result.detail).toBe('offline')
    expect(engine.isDirty('newest')).toBe(true)
  })

  it('resolves a conflict result when the server moved past the version', async () => {
    const server = new FakeServer('theirs', 2)
    const { engine } = makeEngine(server)
    engine.noteSaved('what we read', 1)

    engine.schedule('mine')
    const flushed = engine.flush('mine')
    await server.settleNext() // expected 1, server holds 2 -> conflict
    const result = await flushed

    expect(result.ok).toBe(false)
    expect(result.status).toBe('conflict')
    expect(result.conflict).toEqual({ serverVersion: 2, serverBody: 'theirs' })
  })
})

describe('createSaveEngine: a lost successful response is adopted, not re-fought', () => {
  it('adopts the server version when a stale conflict carries the exact body we wrote', async () => {
    const server = new FakeServer('base', 1)
    const states: SaveStateName[] = []
    const engine = createSaveEngine({
      write: server.write,
      onState: (state) => states.push(state),
      isConflict,
    })
    engine.noteSaved('base', 1)

    engine.schedule('my edit')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    // The server actually applied 'my edit' at version 2, but the ack was lost; the write
    // (which expected version 1) comes back as a stale 409 whose server_body is exactly the
    // body it was carrying.
    server.version = 2
    server.body = 'my edit'
    await server.settleNext()

    expect(engine.conflict()).toBeNull() // no conflict dialog over identical text
    expect(engine.version()).toBe(2) // the server's version is adopted as confirmed
    expect(engine.lastSaved()).toBe('my edit')
    expect(engine.isDirty('my edit')).toBe(false)
    expect(states.at(-1)).toBe('saved')
    expect(states).not.toContain('conflict')
  })
})

describe('decideServerSync: a settled server op never resets over unresolved local work', () => {
  it('adopts when the engine is clean and the editor matches the confirmed body', () => {
    const server = new FakeServer('base', 1)
    const { engine } = makeEngine(server)
    expect(decideServerSync(engine, 'base')).toBe('adopt')
  })

  it('skips while a debounced write is pending: the racing write will conflict on its own', () => {
    const server = new FakeServer('base', 1)
    const { engine } = makeEngine(server)
    engine.schedule('local edit')
    expect(engine.pending()).toBe(true)
    expect(decideServerSync(engine, 'local edit')).toBe('skip')
  })

  it('skips while a write is in flight', async () => {
    const server = new FakeServer('base', 1)
    const { engine } = makeEngine(server)
    engine.schedule('local edit')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    expect(engine.saving()).toBe(true)
    expect(decideServerSync(engine, 'local edit')).toBe('skip')
    await server.settleNext()
  })

  it('skips when a conflict is already open', async () => {
    const server = new FakeServer('newer text', 2)
    const { engine } = makeEngine(server)
    engine.noteSaved('what the editor read', 1)
    engine.schedule('mine')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    await server.settleNext() // conflict
    expect(engine.conflict()).not.toBeNull()
    expect(decideServerSync(engine, 'mine')).toBe('skip')
  })

  it('raises a conflict when idle unsaved local text exists (a failed flush left it dirty)', async () => {
    // A pass that settles while the engine holds idle unsaved text (for example an earlier
    // fire-and-forget flush that failed) must not reset the editor: reconcile instead.
    const server = new FakeServer('base', 1)
    const { engine } = makeEngine(server)
    engine.schedule('local edit')
    const flushed = engine.flush('local edit')
    await server.failNext('offline')
    await flushed

    expect(engine.saving()).toBe(false)
    expect(engine.pending()).toBe(false)
    expect(engine.conflict()).toBeNull()
    expect(engine.isDirty('local edit')).toBe(true)
    expect(decideServerSync(engine, 'local edit')).toBe('conflict')
  })

  it('skips while a write is in flight even when the fetched body equals the editor', async () => {
    // The equality/in-flight race (PLA-289): a body-neutral op refetches a server body that
    // happens to equal the editor's bytes while an unrelated write is still in flight. The
    // `editorShowsServer` flag must never override the skip - that write can still commit a
    // different body server-side, so adopting a baseline now would be a silent divergence.
    const server = new FakeServer('S', 0)
    const { engine } = makeEngine(server)
    engine.schedule('A')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    expect(engine.saving()).toBe(true)
    expect(decideServerSync(engine, 'S', /* editorShowsServer */ true)).toBe('skip')
    await server.settleNext()
  })

  it('adopts on equality once no write can commit, even if the engine reads dirty', () => {
    // The contract of `editorShowsServer`: with the pipeline idle (no in-flight/pending write
    // and no conflict), a freshly-read baseline the editor already shows is authoritative and
    // is adopted, promoting what a dirtiness check alone would have called a conflict. It can
    // only ever turn a would-be conflict into adopt, never a skip - skip is decided first.
    const server = new FakeServer('S', 1)
    const { engine } = makeEngine(server)
    // Idle, but a divergent local body reads dirty on its own.
    expect(engine.saving()).toBe(false)
    expect(engine.pending()).toBe(false)
    expect(engine.isDirty('diverged')).toBe(true)
    expect(decideServerSync(engine, 'diverged', /* editorShowsServer */ false)).toBe('conflict')
    expect(decideServerSync(engine, 'diverged', /* editorShowsServer */ true)).toBe('adopt')
  })
})

describe('reconciliation: an equality sync never invalidates an in-flight save that can still commit', () => {
  // The exact PLA-289 blocker from review 5000931997. `syncEditorFromServer` used to adopt a
  // freshly-read baseline whose bytes equalled the editor *before* consulting whether a body
  // write was still unresolved. That noteSaved bumped the write epoch and could report Saved,
  // but it could not stop the in-flight request from committing server-side - so the editor
  // and the server silently diverged. This drives the fixed decision the workspace now uses.
  function reconcileFromServer(
    engine: ReturnType<typeof createSaveEngine>,
    localBody: string,
    serverBody: string,
    serverVersion: number,
    editorShowsServer: boolean,
  ): 'adopt' | 'skip' | 'conflict' {
    const decision = decideServerSync(engine, localBody, editorShowsServer)
    if (decision === 'skip') return decision
    if (decision === 'adopt') {
      engine.noteSaved(serverBody, serverVersion)
      return decision
    }
    engine.forceConflict(serverBody, serverVersion)
    return decision
  }

  it('S@v0 -> A in flight -> revert S -> body-neutral sync reads S@v0 -> A lands -> corrective S -> Saved', async () => {
    const server = new FakeServer('S', 0)
    const { engine, states } = makeEngine(server)

    engine.schedule('A')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    expect(server.inFlight()).toBe(1) // A in flight, expecting version 0

    // Undo back to the saved text while A is still in the air; a corrective S write is owed.
    engine.schedule('S')

    // A body-neutral op (a rejected suggestion) settles and syncs: the refetch still sees
    // S@v0, so the editor bytes equal the server bytes. The reconciliation must NOT adopt.
    const decision = reconcileFromServer(engine, 'S', 'S', 0, /* editorShowsServer */ true)
    expect(decision).toBe('skip')
    expect(engine.lastSaved()).toBe('S') // no false noteSaved(S, v0) reset the baseline
    expect(engine.conflict()).toBeNull() // and it did not spuriously raise a conflict
    expect(engine.isDirty('S')).toBe(true) // the corrective S write is still owed

    await server.settleNext() // A commits: the server now holds A@v1
    expect(server.body).toBe('A')
    expect(server.version).toBe(1)
    expect(engine.lastSaved()).toBe('A')
    // Never claimed Saved while the editor showed S and the server held A.
    expect(names(states)).not.toContain('saved')

    // The corrective write of S goes out after A, in order, and converges the two on S@v2.
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    expect(server.inFlight()).toBe(1)
    await server.settleNext()

    expect(server.body).toBe('S')
    expect(server.version).toBe(2)
    expect(engine.isDirty('S')).toBe(false)
    expect(names(states).at(-1)).toBe('saved') // Saved only now, at true convergence
    expect(names(states).filter((state) => state === 'saved')).toHaveLength(1)
  })

  it('a no-op external sync whose fetched body equals the editor does not adopt mid-write', async () => {
    // A generic external/no-op sync (not the revert path): the fetched server body equals the
    // editor's current bytes while a different write is still in flight. Equality must not
    // shortcut to adopt, and the in-flight write must be left to settle and confirm on its own.
    const server = new FakeServer('', 0)
    const { engine, states } = makeEngine(server)

    engine.schedule('typed')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    expect(server.inFlight()).toBe(1) // 'typed' in flight

    // The editor shows exactly what a refetch would report, but the write is unresolved.
    const decision = reconcileFromServer(engine, 'typed', 'typed', 7, /* editorShowsServer */ true)
    expect(decision).toBe('skip')
    expect(engine.version()).toBe(0) // the fetched version 7 was NOT adopted mid-write
    expect(engine.conflict()).toBeNull()

    await server.settleNext() // the write confirms 'typed' on its own terms
    expect(server.body).toBe('typed')
    expect(engine.lastSaved()).toBe('typed')
    expect(engine.isDirty('typed')).toBe(false)
    expect(names(states).at(-1)).toBe('saved')
  })
})

/**
 * PLA-315: the beforeunload guard must consult `isDirty()` at each state of
 * the save engine. These tests drive the engine through real state transitions
 * with the FakeServer, verifying that `isDirty()` returns the correct value
 * at every point the guard would check.
 */
describe('isDirty: beforeunload guard contract', () => {
  it('is clean when saved and editor matches', async () => {
    const server = new FakeServer('saved text', 1)
    const { engine } = makeEngine(server)
    expect(engine.isDirty('saved text')).toBe(false)
  })

  it('is dirty during debounce (scheduled but not yet written)', async () => {
    const server = new FakeServer('', 0)
    const { engine } = makeEngine(server)
    engine.schedule('new typing')
    expect(engine.isDirty('new typing')).toBe(true)
  })

  it('is dirty while a write is in flight', async () => {
    const server = new FakeServer('', 0)
    const { engine } = makeEngine(server)
    engine.schedule('content')
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS)
    await flushMicrotasks()
    expect(server.inFlight()).toBe(1)
    expect(engine.isDirty('content')).toBe(true)
  })

  it('is dirty after a failed save', async () => {
    const server = new FakeServer('', 0)
    const { engine, states } = makeEngine(server)
    engine.schedule('will fail')
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS)
    await flushMicrotasks()
    await server.failNext()
    expect(names(states).at(-1)).toBe('error')
    expect(engine.isDirty('will fail')).toBe(true)
  })

  it('is dirty with an unresolved conflict', async () => {
    const server = new FakeServer('', 0)
    const { engine } = makeEngine(server)
    engine.schedule('local text')
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS)
    await flushMicrotasks()
    server.version = 5
    server.body = 'server moved'
    await server.settleNext()
    expect(engine.conflict()).not.toBeNull()
    expect(engine.isDirty('local text')).toBe(true)
  })

  it('is clean after a successful authoritative save', async () => {
    const server = new FakeServer('', 0)
    const { engine } = makeEngine(server)
    engine.schedule('typed')
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS)
    await flushMicrotasks()
    await server.settleNext()
    expect(engine.isDirty('typed')).toBe(false)
  })

  it('is clean after conflict resolution and successful save', async () => {
    const server = new FakeServer('', 0)
    const { engine } = makeEngine(server)
    engine.schedule('local')
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS)
    await flushMicrotasks()
    server.version = 2
    server.body = 'server version'
    await server.settleNext()
    expect(engine.conflict()).not.toBeNull()
    expect(engine.isDirty('local')).toBe(true)

    engine.keepLocal('local')
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS)
    await flushMicrotasks()
    await server.settleNext()
    expect(engine.conflict()).toBeNull()
    expect(engine.isDirty('local')).toBe(false)
  })

  it('is dirty when editor content diverges from last saved', () => {
    const server = new FakeServer('saved', 1)
    const { engine } = makeEngine(server)
    expect(engine.isDirty('saved')).toBe(false)
    expect(engine.isDirty('edited')).toBe(true)
  })

  it('is not dirty when current is null (no editor open)', () => {
    const server = new FakeServer('saved', 1)
    const { engine } = makeEngine(server)
    expect(engine.isDirty(null)).toBe(false)
  })
})

/**
 * PLA-315: the beforeunload handler that the drafts page registers must call
 * `preventDefault` exactly when the engine has unconfirmed state. These tests
 * mount the same wiring pattern the page uses (`engine.isDirty(current)`),
 * dispatch real `beforeunload` events, and prove `defaultPrevented` toggles
 * across all lifecycle states.
 */
describe('beforeunload event wiring (PLA-315)', () => {
  function fireBeforeUnload(): Event {
    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)
    return event
  }

  it('does not prevent navigation when saved and editor matches', () => {
    const server = new FakeServer('saved text', 1)
    const { engine } = makeEngine(server)
    const current = 'saved text'

    const handler = (e: Event) => {
      if (engine.isDirty(current)) e.preventDefault()
    }
    window.addEventListener('beforeunload', handler)
    try {
      expect(fireBeforeUnload().defaultPrevented).toBe(false)
    } finally {
      window.removeEventListener('beforeunload', handler)
    }
  })

  it('prevents navigation during debounce (pending scheduled write)', () => {
    const server = new FakeServer('', 0)
    const { engine } = makeEngine(server)
    const current = 'new typing'

    engine.schedule('new typing')
    const handler = (e: Event) => {
      if (engine.isDirty(current)) e.preventDefault()
    }
    window.addEventListener('beforeunload', handler)
    try {
      expect(fireBeforeUnload().defaultPrevented).toBe(true)
    } finally {
      window.removeEventListener('beforeunload', handler)
    }
  })

  it('prevents navigation while a write is in flight', async () => {
    const server = new FakeServer('', 0)
    const { engine } = makeEngine(server)
    const current = 'content'

    engine.schedule('content')
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS)
    await flushMicrotasks()
    expect(server.inFlight()).toBe(1)

    const handler = (e: Event) => {
      if (engine.isDirty(current)) e.preventDefault()
    }
    window.addEventListener('beforeunload', handler)
    try {
      expect(fireBeforeUnload().defaultPrevented).toBe(true)
    } finally {
      window.removeEventListener('beforeunload', handler)
    }
  })

  it('prevents navigation after a failed save', async () => {
    const server = new FakeServer('', 0)
    const { engine } = makeEngine(server)
    const current = 'will fail'

    engine.schedule('will fail')
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS)
    await flushMicrotasks()
    await server.failNext()

    const handler = (e: Event) => {
      if (engine.isDirty(current)) e.preventDefault()
    }
    window.addEventListener('beforeunload', handler)
    try {
      expect(fireBeforeUnload().defaultPrevented).toBe(true)
    } finally {
      window.removeEventListener('beforeunload', handler)
    }
  })

  it('prevents navigation with an unresolved conflict', async () => {
    const server = new FakeServer('', 0)
    const { engine } = makeEngine(server)
    const current = 'local text'

    engine.schedule('local text')
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS)
    await flushMicrotasks()
    server.version = 5
    server.body = 'server moved'
    await server.settleNext()
    expect(engine.conflict()).not.toBeNull()

    const handler = (e: Event) => {
      if (engine.isDirty(current)) e.preventDefault()
    }
    window.addEventListener('beforeunload', handler)
    try {
      expect(fireBeforeUnload().defaultPrevented).toBe(true)
    } finally {
      window.removeEventListener('beforeunload', handler)
    }
  })

  it('does not prevent navigation after a successful authoritative save', async () => {
    const server = new FakeServer('', 0)
    const { engine } = makeEngine(server)
    const current = 'typed'

    engine.schedule('typed')
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS)
    await flushMicrotasks()
    await server.settleNext()

    const handler = (e: Event) => {
      if (engine.isDirty(current)) e.preventDefault()
    }
    window.addEventListener('beforeunload', handler)
    try {
      expect(fireBeforeUnload().defaultPrevented).toBe(false)
    } finally {
      window.removeEventListener('beforeunload', handler)
    }
  })

  it('does not prevent navigation after conflict resolution and confirmed save', async () => {
    const server = new FakeServer('', 0)
    const { engine } = makeEngine(server)
    const current = 'local'

    engine.schedule('local')
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS)
    await flushMicrotasks()
    server.version = 2
    server.body = 'server version'
    await server.settleNext()
    expect(engine.conflict()).not.toBeNull()

    engine.keepLocal('local')
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS)
    await flushMicrotasks()
    await server.settleNext()

    const handler = (e: Event) => {
      if (engine.isDirty(current)) e.preventDefault()
    }
    window.addEventListener('beforeunload', handler)
    try {
      expect(fireBeforeUnload().defaultPrevented).toBe(false)
    } finally {
      window.removeEventListener('beforeunload', handler)
    }
  })

  it('removing the listener stops preventing navigation even when dirty', () => {
    const server = new FakeServer('', 0)
    const { engine } = makeEngine(server)
    const current = 'unsaved work'

    engine.schedule('unsaved work')

    const handler = (e: Event) => {
      if (engine.isDirty(current)) e.preventDefault()
    }
    window.addEventListener('beforeunload', handler)
    expect(fireBeforeUnload().defaultPrevented).toBe(true)

    window.removeEventListener('beforeunload', handler)
    expect(fireBeforeUnload().defaultPrevented).toBe(false)
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
