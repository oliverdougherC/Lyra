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
 * Three invariants close the stale-write race (PLA-289):
 *
 *  1. At most one write owns the pipeline at a time. A change that arrives while a write
 *     is in flight is coalesced into the newest pending body and written next, never as a
 *     competing request. `flush()` joins the same pipeline; it does not open a second one.
 *     So an older write can never land after a newer one from this editor.
 *
 *  2. The engine tracks the newest *desired* editor body independently of the body it last
 *     confirmed (`lastSaved`) and of the body a write is currently carrying. Reverting to
 *     the previously-saved text while a different write is in flight therefore cannot make
 *     the engine believe nothing is owed: it still owes a write of the desired body once
 *     the in-flight one lands, and it never reports `saved` while editor and server differ.
 *
 *  3. Every write states the `expected_version` it last saw and the server refuses it if
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

/**
 * What an explicit `flush()` proves about the newest local body. A caller about to run a
 * server operation that reads the body server-side (a pass, a review, an export, a
 * snapshot) must not proceed unless `ok` is true: only then is the text on screen the text
 * on disk. `error` and `conflict` distinguish the two ways it is not, so the caller can
 * surface the actionable save state or leave the reconciliation dialog to do its work.
 */
export interface FlushResult {
  /** True only when the server has confirmed the newest local body at `version`. */
  ok: boolean
  status: 'saved' | 'error' | 'conflict'
  /** The authoritative version the server now holds for this editor's confirmed body. */
  version: number
  /** The failure reason, when `status` is `error`. */
  detail?: string
  /** The server's version and body, when `status` is `conflict`. */
  conflict?: SaveConflict
}

export interface SaveEngine {
  /** A change arrived: mark dirty and start the rest timer over. */
  schedule(content: string): void
  /**
   * Write the newest content through now and resolve once the pipeline is idle again with
   * a verdict on whether it landed. Joins the current write rather than starting a
   * competing one. `null` content means nothing is open, so it only drains what is already
   * owed. The result lets a body-dependent caller prove the newest text is authoritative
   * before it acts on it.
   */
  flush(content: string | null): Promise<FlushResult>
  /** Drop scheduled and pending work without running it. */
  cancel(): void
  /** A debounced write is scheduled but has not run. */
  pending(): boolean
  /** A write currently owns the pipeline. */
  saving(): boolean
  /** Mark `content` at `version` as what the server holds (seeding and authoritative ops). */
  noteSaved(content: string, version: number): void
  /**
   * Raise a stale-version conflict from outside a write: an authoritative server operation
   * (an AI pass, an accepted suggestion, a restore) moved the body to `serverVersion` while
   * this editor still holds unsaved local text. The local text is kept and the student
   * reconciles it, exactly as a refused autosave would - never silently overwritten.
   */
  forceConflict(serverBody: string, serverVersion: number): void
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
  // The newest body the editor wants persisted. Tracked independently of `lastSaved` and of
  // the in-flight write's body: the pipeline's job is to make the server hold exactly this,
  // and nothing is owed only when `desired === lastSaved`. Reverting the editor to the
  // previously-saved text sets this back to `lastSaved`, but if a write is in flight moving
  // the server elsewhere, a corrective write of `desired` is still owed once it lands.
  let desired = ''
  // The newest body the student wants persisted that the server has not confirmed. null
  // means the confirmed body is already the newest, so there is nothing to write. Kept in
  // step with `desired !== lastSaved` whenever the pipeline settles.
  let pendingBody: string | null = null
  // The single owner of the pipeline: the write in flight, or null when idle.
  let inFlight: Promise<void> | null = null
  // The body that in-flight write is carrying, so a change arriving mid-write can tell
  // whether the pipeline is already heading where the editor now wants to go.
  let inFlightBody: string | null = null
  // Bumped whenever the confirmed baseline is reset out from under an in-flight write -
  // a conflict raised or resolved (`forceConflict`/`keepLocal`/`takeServer`) or a seed
  // (`noteSaved`). A write captures the epoch when it starts; if it changed by the time the
  // write settles, the write acted on a superseded baseline and its result is ignored. This
  // is what stops a late stale response from resurrecting an already-resolved conflict or
  // rolling the confirmed body/version backward (PLA-289 round 3).
  let writeEpoch = 0
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
    // `saved` is only honest when the editor's desired body is exactly what the server
    // confirmed. `pendingBody` and `desired !== lastSaved` say the same thing when idle;
    // both are checked so a missed sync can never leak a false `saved`.
    if (pendingBody !== null || desired !== lastSaved) {
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
   * folded into this one. On success advances `lastSaved`/`version` and recomputes what is
   * still owed against the newest `desired` body; on failure keeps the body pending
   * (retryable); on a version conflict records it and keeps the local body.
   */
  function beginWrite(): Promise<void> {
    const writing = pendingBody as string
    const base = version
    const epoch = writeEpoch
    inFlightBody = writing
    const run = (async () => {
      let outcome: WriteOutcome | undefined
      let failure: unknown
      let threw = false
      try {
        outcome = await opts.write(writing, base)
      } catch (error) {
        threw = true
        failure = error
      }
      // This write always owns the pipeline (a new one cannot start while one is in flight),
      // so releasing ownership here is unconditional.
      inFlight = null
      inFlightBody = null
      // A conflict raised/resolved or a seed moved the baseline while this write was in the
      // air. Its outcome is about a version the engine no longer stands on, so applying it
      // would either resurrect a resolved conflict or roll the confirmed body backward.
      // Drop it; whatever reset the baseline already set the correct state.
      if (epoch !== writeEpoch) return
      if (!threw) {
        lastSaved = writing
        version = (outcome as WriteOutcome).version
        errorDetail = undefined
        // Recompute against the newest desired body, not merely the body we just wrote: a
        // revert-while-writing leaves `desired` behind `writing`, and that corrective write
        // is still owed.
        pendingBody = desired !== lastSaved ? desired : null
        return
      }
      const detected = opts.isConflict?.(failure) ?? null
      if (detected && detected.serverBody === writing) {
        // The server already holds exactly the body this write was carrying, one version
        // on: our successful response was almost certainly lost (a dropped ack, a retry of
        // an identical write). Adopting `serverVersion` as confirmed is provably safe -
        // the bytes are identical, so nothing is lost - and it spares the student a
        // conflict dialog over text that already matches (PLA-289).
        lastSaved = writing
        version = detected.serverVersion
        errorDetail = undefined
        pendingBody = desired !== lastSaved ? desired : null
      } else if (detected) {
        // Keep pendingBody: the local text is not lost, it is waiting to be reconciled.
        conflict = detected
      } else {
        errorDetail = failure instanceof Error ? failure.message : String(failure)
      }
    })()
    // Assign before reporting so `settle()` sees the write and reports `saving`.
    inFlight = run
    settle()
    return run
  }

  /** Start a write if one is owed and the pipeline is free and unblocked by a conflict. */
  function pump(): void {
    if (conflict || inFlight || pendingBody === null) return
    void beginWrite().then(afterAutoWrite)
  }

  async function drain(): Promise<void> {
    // Drive the server to the newest desired body, one owner throughout: never start a
    // second write while one is in flight. Stops on a conflict (needs the student) or an
    // ordinary failure (leave it dirty+error rather than hot-looping a dead endpoint).
    while (!conflict) {
      if (inFlight) {
        await inFlight.catch(() => undefined)
        continue
      }
      if (desired === lastSaved) {
        pendingBody = null
        break
      }
      pendingBody = desired
      await beginWrite().catch(() => undefined)
      if (conflict || errorDetail !== undefined) break
    }
    settle()
  }

  /** The flush verdict, read off where the pipeline came to rest. */
  function flushResult(): FlushResult {
    if (conflict) return { ok: false, status: 'conflict', version, conflict }
    if (desired === lastSaved) return { ok: true, status: 'saved', version }
    return { ok: false, status: 'error', version, detail: errorDetail }
  }

  return {
    schedule(content: string): void {
      desired = content
      if (conflict) {
        // Still unreconciled: track the newest local text so a resolution can write it,
        // but never auto-save over the server while the conflict stands.
        pendingBody = content === lastSaved ? null : content
        report('conflict')
        return
      }
      // Where the pipeline is already heading: the in-flight write's target if one owns it,
      // otherwise the confirmed body. A write is owed only when the editor wants something
      // other than that destination - which is why reverting to `lastSaved` mid-write still
      // owes a corrective write, because the destination is then the in-flight body, not
      // `lastSaved`.
      const heading = inFlight ? (inFlightBody as string) : lastSaved
      if (content === heading) {
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
    async flush(content): Promise<FlushResult> {
      clearTimeout(timer)
      timer = undefined
      if (content != null) desired = content
      if (conflict) {
        // A flush cannot resolve a conflict; keep the local text tracked and report it.
        if (content != null) pendingBody = content === lastSaved ? null : content
        report('conflict')
        if (inFlight) await inFlight.catch(() => undefined)
        return flushResult()
      }
      await drain()
      return flushResult()
    },
    cancel(): void {
      clearTimeout(timer)
      timer = undefined
      pendingBody = null
      errorDetail = undefined
      // Dropping unsaved work means the editor no longer wants anything the server lacks.
      if (inFlight === null) desired = lastSaved
      if (!conflict) settle()
    },
    pending: () => timer !== undefined,
    saving: () => inFlight !== null,
    noteSaved(content: string, nextVersion: number): void {
      lastSaved = content
      version = nextVersion
      desired = content
      pendingBody = null
      conflict = null
      errorDetail = undefined
      // Seeding is an authoritative baseline reset; a straggler write must not undo it.
      writeEpoch += 1
      settle()
    },
    forceConflict(serverBody: string, serverVersion: number): void {
      conflict = { serverVersion, serverBody }
      // Whatever the editor last wanted is the local text to reconcile; keep it pending so
      // a `keepLocal` resolution has it, and never clear it.
      if (pendingBody === null && desired !== lastSaved) pendingBody = desired
      // Invalidate any write still in flight: it carries the pre-conflict version, and if it
      // happened to land it would advance `lastSaved`/`version` under the conflict and leave
      // the engine in an inconsistent state. The server's CAS refuses it in any case.
      writeEpoch += 1
      report('conflict')
    },
    lastSaved: () => lastSaved,
    version: () => version,
    isDirty(current: string | null): boolean {
      if (conflict !== null || pendingBody !== null || inFlight !== null) return true
      if (timer !== undefined || desired !== lastSaved) return true
      return current != null && current !== lastSaved
    },
    conflict: () => conflict,
    keepLocal(content: string): void {
      if (!conflict) return
      // The conflict is proof the server holds `serverBody` at `serverVersion`; that pair is
      // now the authoritative baseline, NOT the pre-conflict `lastSaved` (which the conflict
      // just disproved). Adopt it as the base so a write of the local text compare-and-swaps
      // against the version the server actually holds.
      const serverBody = conflict.serverBody
      const serverVersion = conflict.serverVersion
      lastSaved = serverBody
      version = serverVersion
      conflict = null
      errorDetail = undefined
      desired = content
      // A write is owed whenever the chosen local text differs from what the server actually
      // holds - decided against `serverBody`, never the stale pre-conflict `lastSaved`. When
      // the bytes already match, adopting without a redundant write is safe and `Saved` is
      // honest at once; otherwise `Saved` is withheld until the corrective write lands.
      pendingBody = content === serverBody ? null : content
      // Invalidate any older write still in flight so its late response cannot resurrect this
      // conflict or clobber the rebased baseline.
      writeEpoch += 1
      settle()
      if (pendingBody === null) return
      if (inFlight) {
        // An older write still owns the pipeline; land the corrective write the moment it
        // releases. `pump` re-checks the state, so a fresh conflict or a write started in the
        // meantime is respected.
        void inFlight.catch(() => undefined).then(pump)
        return
      }
      pump()
    },
    takeServer(): SaveConflict | null {
      if (!conflict) return null
      const resolved = conflict
      lastSaved = resolved.serverBody
      version = resolved.serverVersion
      desired = resolved.serverBody
      pendingBody = null
      conflict = null
      errorDetail = undefined
      // Authoritative baseline reset; a straggler write must not undo the adoption.
      writeEpoch += 1
      settle()
      return resolved
    },
  }
}

/**
 * What to do when an authoritative server operation (an AI pass, an accepted suggestion, a
 * restore) has moved the body and the editor must be reconciled with it:
 *
 * - `adopt`: there is no unresolved local work, so the editor may follow the server - reset
 *   it to the server body and move the version base forward.
 * - `skip`: a write is racing this sync (in flight, or a debounce about to fire). That write
 *   carries the pre-operation version, so the server's compare-and-swap will refuse it and
 *   the engine raises the conflict itself. Leave the editor and the pipeline untouched.
 * - `conflict`: the student has unsaved local text the operation moved under. Raise a
 *   reconciliation and keep their words; never reset over them.
 *
 * Pure and side-effect free so the workspace's reconciliation can be tested against the real
 * engine's state without an editor. The caller has already handled the case where the editor
 * already shows the server's body (only the version base moved); this decides the rest.
 */
export function decideServerSync(
  engine: Pick<SaveEngine, 'conflict' | 'saving' | 'pending' | 'isDirty'>,
  localBody: string,
): 'adopt' | 'skip' | 'conflict' {
  if (engine.conflict() || engine.saving() || engine.pending()) return 'skip'
  if (!engine.isDirty(localBody)) return 'adopt'
  return 'conflict'
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
