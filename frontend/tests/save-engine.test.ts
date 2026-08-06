import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createSaveEngine,
  flushOnHidden,
  SAVE_DEBOUNCE_MS,
  type SaveStateName,
} from '@/lib/drafts/save-engine'

function createEngine(write = vi.fn().mockResolvedValue(undefined)) {
  const states: SaveStateName[] = []
  const engine = createSaveEngine({
    write,
    onState: (state) => states.push(state),
  })
  return { engine, write, states }
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('createSaveEngine', () => {
  it('writes once, 1.5s after the last change, and reports dirty then saving then saved', async () => {
    const { engine, write, states } = createEngine()

    engine.schedule('first')
    engine.schedule('second')

    // The timer started over: nothing has been written yet, and the state is dirty.
    expect(states).toEqual(['dirty'])
    expect(write).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)

    // One write, with the latest content: the debounced autosave, not one per keystroke.
    expect(write).toHaveBeenCalledTimes(1)
    expect(write).toHaveBeenCalledWith('second')
    expect(states).toEqual(['dirty', 'saving', 'saved'])
  })

  it('does nothing when the content has not changed since the last save', async () => {
    const { engine, write, states } = createEngine()
    engine.noteSaved('already there')

    engine.schedule('already there')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS * 2)

    expect(write).not.toHaveBeenCalled()
    expect(states).toEqual([])
    expect(engine.isDirty('already there')).toBe(false)
  })

  it('reports an error with its message when the write fails', async () => {
    const write = vi.fn().mockRejectedValue(new Error('Could not reach the Lyra server.'))
    const { engine, states } = createEngine(write)

    engine.schedule('unsaved work')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)

    expect(states).toEqual(['dirty', 'saving', 'error'])
    expect(engine.isDirty('unsaved work')).toBe(true)
  })

  it('flushes immediately and drops the scheduled write, so nothing is written twice', async () => {
    const { engine, write } = createEngine()

    engine.schedule('pending change')
    expect(engine.pending()).toBe(true)

    await engine.flush('pending change')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS * 2)

    expect(write).toHaveBeenCalledTimes(1)
    expect(write).toHaveBeenCalledWith('pending change')
    expect(engine.pending()).toBe(false)
  })

  it('treats a flush with nothing open as a no-op', async () => {
    const { engine, write } = createEngine()

    await engine.flush(null)

    expect(write).not.toHaveBeenCalled()
  })

  it('marks clean after a successful write and dirty again on the next change', async () => {
    const { engine } = createEngine()

    engine.schedule('one')
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    expect(engine.isDirty('one')).toBe(false)
    expect(engine.lastSaved()).toBe('one')

    engine.schedule('two')
    expect(engine.isDirty('two')).toBe(true)
  })
})

describe('flushOnHidden', () => {
  it('flushes when the tab goes hidden, and only then', () => {
    const flush = vi.fn()
    const detach = flushOnHidden(flush)

    // jsdom reports 'visible' forever, so the test drives the state itself: an own
    // property shadows the prototype getter, and is deleted afterwards to restore it.
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
      // Drops the own property, putting the prototype's real getter back.
      Reflect.deleteProperty(document, 'visibilityState')
    }
  })
})
