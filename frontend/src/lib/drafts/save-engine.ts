/**
 * The draft autosave engine: a debounced writer with an honest save state.
 *
 * Factored as plain functions over an injected `write` so the debounce, the flush, and
 * the state transitions are unit-testable without an editor. The workspace wires it to
 * `PATCH /api/drafts/{id}/body` and to the editor's change events; the engine knows
 * nothing about either.
 *
 * The states are the whole point: a writing tool that cannot say whether the words on
 * screen are the words on disk is not a writing tool. `dirty` means a change is waiting,
 * `saving` means a write is in the air, `saved` means the server has what is on screen,
 * and `error` carries the reason the last write failed.
 */

export type SaveStateName = 'saved' | 'saving' | 'dirty' | 'error'

/** The pause after the last keystroke that counts as a rest. */
export const SAVE_DEBOUNCE_MS = 1500

/** What `window.setTimeout` hands back, named once so the engine never repeats it. */
export type TimerHandle = number

export interface SaveEngine {
  /** A change arrived: mark dirty and start the rest timer over. */
  schedule(content: string): void
  /** Write through now (null content = nothing open, a no-op). */
  flush(content: string | null): Promise<void>
  /** Drop a scheduled write without running it. */
  cancel(): void
  /** A debounced write is scheduled but has not run. */
  pending(): boolean
  /** Mark `content` as what the server holds (guards save short-circuits). */
  noteSaved(content: string): void
  lastSaved(): string
  isDirty(current: string | null): boolean
}

export function createSaveEngine(opts: {
  write: (content: string) => Promise<void>
  onState: (state: SaveStateName, detail?: string) => void
  debounceMs?: number
}): SaveEngine {
  let timer: TimerHandle | undefined
  let lastSaved = ''

  async function doSave(content: string): Promise<void> {
    timer = undefined
    if (content === lastSaved) {
      opts.onState('saved')
      return
    }
    opts.onState('saving')
    try {
      await opts.write(content)
      lastSaved = content
      opts.onState('saved')
    } catch (error) {
      opts.onState('error', (error as Error).message)
    }
  }

  return {
    schedule(content: string): void {
      if (content === lastSaved) return
      opts.onState('dirty')
      clearTimeout(timer)
      timer = window.setTimeout(() => void doSave(content), opts.debounceMs ?? SAVE_DEBOUNCE_MS)
    },
    async flush(content) {
      clearTimeout(timer)
      timer = undefined
      if (content == null) return
      await doSave(content)
    },
    cancel(): void {
      clearTimeout(timer)
      timer = undefined
    },
    pending: () => timer !== undefined,
    noteSaved(content: string): void {
      lastSaved = content
    },
    lastSaved: () => lastSaved,
    isDirty(current: string | null): boolean {
      return timer !== undefined || (current != null && current !== lastSaved)
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
