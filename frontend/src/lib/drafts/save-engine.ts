/**
 * The draft autosave engine: a single-owner, versioned writer with an honest save state.
 *
 * Factored as plain functions over an injected `write` so the debounce, the flush, the
 * ordering, and the state transitions are unit-testable without an editor. The workspace
 * wires it to `PATCH /api/drafts/{id}/body` and to the editor's change events; the engine
 * knows nothing about either.
 *
 * The states are the whole point: a writing tool that cannot say whether the words on
 * screen are the words on disk is not a writing tool. `dirty` means a change is waiting,
 * `saving` means a write is in the air, `saved` means the server has confirmed exactly
 * what is on screen at the version it holds, `error` carries the reason the last write
 * failed, and `conflict` means the server moved on under us and the two versions must be
 * reconciled before anything can be called saved.
 *
 * Two invariants close the stale-write race (PLA-289):
 *
 *  1. At most one write owns the pipeline at a time. A change that arrives while a write
 *     is in flight is coalesced into the newest pending body and written next, never as a
 *     competing request. `flush()` joins the same pipeline; it does not open a second one.
 *     So an older write can never land after a newer one from this editor.
 *
 *  2. Every write states the `expected_version` it last saw and the server refuses it if
 *     the stored body moved past that version. A second tab, a slow retry, or an AI pass
 *     that rewrote the body therefore produces a deterministic conflict here rather than a
 *     silent overwrite - and the local text is kept for the student to reconcile.
 */

export type SaveStateName = 'saved' | 'saving' | 'dirty' | 'error' | 'conflict'

/** The pause after the last keystroke that counts as a rest. */
export const SAVE_DEBOUNCE_MS = 1500

/** What `window.setTimeout` hands back, named once so the engine never repeats it. */
export type TimerHandle = number

/** What a successful write reports back: the new authoritative version of the body. */
export interface WriteOutcome {
  version: number
}

/** The server's side of a stale-version conflict, kept so the student can reconcile it. */
export interface SaveConflict {
  serverVersion: number
  serverBody: string
}

export interface SaveEngine {
  /** A change arrived: mark dirty and start the rest timer over. */
  schedule(content: string): void
  /**
   * Write the newest content through now and resolve once the pipeline is idle again
   * (the newest body is durably stored, or a write failed/conflicted). Joins the current
   * write rather than starting a competing one. `null` content means nothing is open.
   */
  flush(content: string | null): Promise<void>
  /** Drop scheduled and pending work without running it. */
  cancel(): void
  /** A debounced write is scheduled but has not run. */
  pending(): boolean
  /** Mark `content` at `version` as what the server holds (seeding and authoritative ops). */
  noteSaved(content: string, version: number): void
  lastSaved(): string
  /** The version the server confirmed for `lastSaved`, echoed as `expected_version`. */
  version(): number
  isDirty(current: string | null): boolean
  /** The unresolved stale-version conflict, or null. */
  conflict(): SaveConflict | null
  /** Reconcile by keeping the local text: rebase onto the server version and write it. */
  keepLocal(content: string): void
  /**
   * Reconcile by taking the server's text as the new saved base. Returns the conflict so
   * the caller can reset the editor to `serverBody`, or null when there was none.
   */
  takeServer(): SaveConflict | null
}

export function createSaveEngine(opts: {
  write: (content: string, expectedVersion: number) => Promise<WriteOutcome>
  onState: (state: SaveStateName, detail?: string) => void
  /** Classify a rejected write as a stale-version conflict, or null for an ordinary failure. */
  isConflict?: (error: unknown) => SaveConflict | null
  debounceMs?: number
}): SaveEngine {
  const debounceMs = opts.debounceMs ?? SAVE_DEBOUNCE_MS
  let timer: TimerHandle | undefined
  let lastSaved = ''
  let version = 0
  // The newest body the student wants persisted that the server has not confirmed. null
  // means the confirmed body is already the newest, so there is nothing to write.
  let pendingBody: string | null = null
  // The single owner of the pipeline: the write in flight, or null when idle.
  let inFlight: Promise<void> | null = null
  let conflict: SaveConflict | null = null
  let errorDetail: string | undefined
  // The state is a stream of transitions, not of keystrokes: the fortieth dirty in a row
  // says nothing the first did not.
  let reported: SaveStateName = 'saved'
  let reportedDetail: string | undefined

  function report(state: SaveStateName, detail?: string): void {
    if (state === reported && detail === reportedDetail) return
    reported = state
    reportedDetail = detail
    opts.onState(state, detail)
  }

  /** Emit the one true state derived from where the pipeline currently stands. */
  function settle(): void {
    if (conflict) {
      report('conflict')
      return
    }
    if (inFlight) {
      report('saving')
      return
    }
    if (pendingBody !== null) {
      if (errorDetail !== undefined) report('error', errorDetail)
      else report('dirty')
      return
    }
    report('saved')
  }

  function armTimer(): void {
    clearTimeout(timer)
    timer = window.setTimeout(() => {
      timer = undefined
      void kick()
    }, debounceMs)
  }

  /** The debounce fired: start a write if the pipeline is free and there is one owed. */
  function kick(): void {
    if (inFlight || conflict || pendingBody === null) {
      settle()
      return
    }
    void beginWrite().then(afterAutoWrite)
  }

  /** After a debounce-driven write settles, re-arm for newer content or a retry. */
  function afterAutoWrite(): void {
    settle()
    if (!conflict && pendingBody !== null) armTimer()
  }

  /**
   * Own the pipeline for exactly one write of the current `pendingBody` at the current
   * `version`. Captures both up front so a change arriving mid-write is written next, not
   * folded into this one. On success advances `lastSaved`/`version`; on failure keeps the
   * body pending (retryable); on a version conflict records it and keeps the local body.
   */
  function beginWrite(): Promise<void> {
    const writing = pendingBody as string
    const base = version
    const run = (async () => {
      try {
        const outcome = await opts.write(writing, base)
        lastSaved = writing
        version = outcome.version
        errorDetail = undefined
        // Only clear the pending marker if nothing newer arrived while we were writing.
        if (pendingBody === writing) pendingBody = null
      } catch (error) {
        const detected = opts.isConflict?.(error) ?? null
        if (detected) {
          // Keep pendingBody: the local text is not lost, it is waiting to be reconciled.
          conflict = detected
        } else {
          errorDetail = error instanceof Error ? error.message : String(error)
        }
      } finally {
        inFlight = null
      }
    })()
    // Assign before reporting so `settle()` sees the write and reports `saving`.
    inFlight = run
    settle()
    return run
  }

  function setPendingFrom(content: string): void {
    if (content === lastSaved) {
      pendingBody = null
      errorDetail = undefined
    } else {
      pendingBody = content
    }
  }

  async function drain(): Promise<void> {
    // Drive the newest pending body to the server, one owner throughout: never start a
    // second write while one is in flight. Stops on a conflict (needs the student) or an
    // ordinary failure (leave it dirty+error rather than hot-looping a dead endpoint).
    while (!conflict && pendingBody !== null) {
      if (inFlight) {
        await inFlight.catch(() => undefined)
        continue
      }
      await beginWrite().catch(() => undefined)
      if (conflict || errorDetail !== undefined) break
    }
    settle()
  }

  return {
    schedule(content: string): void {
      if (conflict) {
        // Still unreconciled: track the newest local text so a resolution can write it,
        // but never auto-save over the server while the conflict stands.
        setPendingFrom(content)
        report('conflict')
        return
      }
      if (content === lastSaved) {
        // Reverted to the saved text: nothing is owed. A write already in flight will
        // settle to saved on its own when it lands.
        pendingBody = null
        errorDetail = undefined
        clearTimeout(timer)
        timer = undefined
        settle()
        return
      }
      pendingBody = content
      // A fresh keystroke supersedes a stale error label; the retry rides the new content.
      errorDetail = undefined
      settle()
      armTimer()
    },
    async flush(content) {
      clearTimeout(timer)
      timer = undefined
      if (content == null) {
        // Nothing open, but a write already owning the pipeline must still be waited on so
        // the caller's "the newest is stored" promise is honest.
        if (inFlight) await inFlight.catch(() => undefined)
        settle()
        return
      }
      if (conflict) {
        // A flush cannot resolve a conflict; keep the local text tracked and report it.
        setPendingFrom(content)
        report('conflict')
        if (inFlight) await inFlight.catch(() => undefined)
        return
      }
      setPendingFrom(content)
      await drain()
    },
    cancel(): void {
      clearTimeout(timer)
      timer = undefined
      pendingBody = null
      errorDetail = undefined
      if (!conflict) settle()
    },
    pending: () => timer !== undefined,
    noteSaved(content: string, nextVersion: number): void {
      lastSaved = content
      version = nextVersion
      if (pendingBody === content) pendingBody = null
      conflict = null
      errorDetail = undefined
      settle()
    },
    lastSaved: () => lastSaved,
    version: () => version,
    isDirty(current: string | null): boolean {
      if (conflict !== null || pendingBody !== null || inFlight !== null) return true
      return timer !== undefined || (current != null && current !== lastSaved)
    },
    conflict: () => conflict,
    keepLocal(content: string): void {
      if (!conflict) return
      // Rebase onto what the server now holds so our write matches, then overwrite it with
      // the local text. This is a deliberate choice, so write it now rather than on a rest.
      version = conflict.serverVersion
      conflict = null
      errorDetail = undefined
      setPendingFrom(content)
      settle()
      if (pendingBody !== null && !inFlight) {
        void beginWrite().then(afterAutoWrite)
      }
    },
    takeServer(): SaveConflict | null {
      if (!conflict) return null
      const resolved = conflict
      lastSaved = resolved.serverBody
      version = resolved.serverVersion
      pendingBody = null
      conflict = null
      errorDetail = undefined
      settle()
      return resolved
    },
  }
}

/**
 * Flush when the tab goes hidden, which is the last moment a write can still be sent
 * before the page is frozen or gone. Returns the detach for unmount.
 */
export function flushOnHidden(flush: () => void): () => void {
  const onVisibilityChange = () => {
    if (document.visibilityState === 'hidden') flush()
  }
  document.addEventListener('visibilitychange', onVisibilityChange)
  return () => document.removeEventListener('visibilitychange', onVisibilityChange)
}
