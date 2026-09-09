/**
 * Teardown and remount recovery for the save engine (PLA-513, from the integration review
 * probes): what a disposed engine still owes, and what a remounted document does about it.
 *
 * - A teardown flush that is already running completes the newest-body pipeline after
 *   `dispose` - it owns the pipeline, so it finishes it, but re-arms no timer and admits
 *   no new owner. This is what stops newer text from being dropped at unmount.
 * - A confirmed teardown save releases its update-safety blocker; a failed one keeps it,
 *   because the work is genuinely unconfirmed.
 * - After a failed final flush, a remount of the same document recovers the unconfirmed
 *   bytes: the next engine re-ows them under the document's shared owner, visibly
 *   unsaved until the write lands. Keeping the bytes only in an unreachable disposed
 *   engine - or the blocker only in an orphaned symbol - would not be recovery.
 *
 * The page-level counterparts (StrictMode, hidden mount, remount of the real workspace)
 * live in `draft-lifecycle.test.tsx`.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { createSaveEngine, SAVE_DEBOUNCE_MS } from '@/lib/drafts/save-engine'
import { saveOwnerFor, saveSessionEngine, saveSessionFor } from '@/lib/drafts/save-session'
import { assertUpdateSafe } from '@/lib/update-safety'

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

it('an already requested teardown flush drains the newest body after an older write', async () => {
  let finishFirst!: (value: { version: number }) => void
  const write = vi
    .fn()
    .mockImplementationOnce(
      () =>
        new Promise<{ version: number }>((resolve) => {
          finishFirst = resolve
        }),
    )
    .mockResolvedValue({ version: 3 })
  const engine = createSaveEngine({ write, onState: vi.fn() })
  engine.noteSaved('saved', 1)
  engine.schedule('older edit')
  await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS) // the autosave is in flight
  engine.schedule('newest edit')
  const flushing = engine.flush('newest edit')
  engine.dispose()

  // The older write settles long after the surface left: the running flush keeps driving
  // the pipeline to the newest body.
  finishFirst({ version: 2 })
  await vi.advanceTimersByTimeAsync(0)
  const result = await flushing

  expect(write.mock.calls.map((call) => call[0])).toEqual(['older edit', 'newest edit'])
  expect(result.ok).toBe(true)
  expect(engine.lastSaved()).toBe('newest edit')
  expect(engine.pendingContent()).toBeNull()
  expect(vi.getTimerCount()).toBe(0) // nothing re-armed for a page that is gone
})

it('a confirmed teardown save releases its update-safety blocker', async () => {
  let finish!: (value: { version: number }) => void
  const write = vi.fn(
    () =>
      new Promise<{ version: number }>((resolve) => {
        finish = resolve
      }),
  )
  const engine = createSaveEngine({ write, onState: vi.fn() })
  engine.noteSaved('saved', 1)
  engine.schedule('latest')
  const flushing = engine.flush('latest')
  engine.dispose()

  finish({ version: 2 })
  await flushing

  expect(engine.isDirty('latest')).toBe(false)
  // The confirmed final save settled the gate even though the engine is disposed: the
  // blocker is gone, and no timer was left behind.
  expect(() => assertUpdateSafe()).not.toThrow()
  expect(vi.getTimerCount()).toBe(0)
})

it('recovers pending bytes on remount: one engine, visibly unsaved until the recovered write lands', async () => {
  // The per-document session: a failed final flush does not retire the session - the
  // document's engine still owes those bytes, and the gate's owner symbol follows the
  // document, not the engine. A remount reattaches to this same engine rather than
  // cloning its state into a second writer.
  const key = '7'
  const owner = saveOwnerFor(key)
  const write = vi
    .fn()
    .mockResolvedValueOnce({ version: 2 })
    .mockImplementationOnce(() => Promise.reject(new Error('offline')))
    .mockResolvedValue({ version: 3 })
  const states: string[] = []
  const session = saveSessionFor(key, () =>
    createSaveEngine({
      write,
      onState: (state) => states.push(state),
      owner,
    }),
  )
  const engine = session.engine

  // Mount one: an autosave lands, the editor moves on, and the final flush fails.
  engine.noteSaved('saved', 1)
  engine.schedule('first edit')
  await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
  expect(write).toHaveBeenCalledTimes(1)
  engine.schedule('second edit')
  session.detach()
  await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)

  // The teardown flush failed: the bytes are still owed, and the gate still blocks.
  // The session is retained (not retired) because pending work stands.
  expect(engine.pendingContent()).toBe('second edit')
  expect(engine.isDirty('second edit')).toBe(true)
  expect(saveSessionEngine(key) === engine).toBe(true)
  expect(() => assertUpdateSafe()).toThrow()

  // Mount two of the same document: the very same engine is reattached, so its pending
  // work and confirmed version are intact. The engine stays visibly unsaved until the
  // recovered write lands...
  session.attach((state) => states.push(state))
  expect(engine.pendingContent()).toBe('second edit')
  expect(engine.isDirty('second edit')).toBe(true)

  // ...and the recovered write eventually lands (allowing generous time for the backoff),
  // and carries the confirmed version forward.
  await vi.advanceTimersByTimeAsync(60_000)
  expect(write).toHaveBeenCalledTimes(3) // write #1 (ok), #2 (fail), #3 (ok)
  expect(write).toHaveBeenLastCalledWith('second edit', 2)
  expect(engine.lastSaved()).toBe('second edit')
  expect(engine.isDirty('second edit')).toBe(false)

  // The recovered save cleared the gate: the blocker is resolvable, not permanent.
  expect(() => assertUpdateSafe()).not.toThrow()

  // Clean up: detach with nothing owed and let the session retire.
  session.detach()
  await vi.advanceTimersByTimeAsync(0)
})

it('a successful teardown flush retires the session: no retained engine after detach', async () => {
  const key = '7'
  const write = vi.fn().mockResolvedValue({ version: 2 })
  const session = saveSessionFor(key, (notify) =>
    createSaveEngine({ write, onState: notify, owner: saveOwnerFor(key) }),
  )
  const engine = session.engine

  engine.noteSaved('saved', 1)
  engine.schedule('only edit')
  session.detach()
  // The final flush confirms the write: the session retires - the engine is disposed and
  // the registry entry removed. A rapid remount seeds from the query, not a stale engine.
  await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
  expect(saveSessionEngine(key)).toBeNull()
  expect(engine.disposed()).toBe(true)
  expect(vi.getTimerCount()).toBe(0) // no background retry owner after detachment
})
