import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createSaveEngine } from '@/lib/drafts/save-engine'
import { saveSessionFor } from '@/lib/drafts/save-session'

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

it('coalesces outage edits behind the existing retry deadline', async () => {
  const write = vi.fn().mockRejectedValue(new Error('offline'))
  const engine = createSaveEngine({ write, onState: vi.fn() })
  engine.noteSaved('saved', 1)
  engine.schedule('first')
  await vi.advanceTimersByTimeAsync(3500)
  expect(write).toHaveBeenCalledTimes(2)
  engine.schedule('newest')
  await vi.advanceTimersByTimeAsync(1500)
  expect(write).toHaveBeenCalledTimes(2)
  await vi.advanceTimersByTimeAsync(2500)
  expect(write).toHaveBeenCalledTimes(3)
  expect(write).toHaveBeenLastCalledWith('newest', 1)
  engine.cancel()
})

it('detaching an older view preserves the active view and its desired body', async () => {
  const write = vi.fn().mockResolvedValue({ version: 2 })
  const create = (onState: (state: import('@/lib/drafts/save-engine').SaveStateName) => void) =>
    createSaveEngine({ write, onState })
  const older = saveSessionFor('integration-lease', create)
  const current = saveSessionFor('integration-lease', create)
  older.engine.noteSaved('saved', 1)
  older.attach(vi.fn())
  const state = vi.fn()
  current.attach(state)
  current.engine.schedule('newest')
  older.detach()
  expect(write).not.toHaveBeenCalled()
  await vi.advanceTimersByTimeAsync(1500)
  expect(write).toHaveBeenCalledWith('newest', 1)
  expect(state).toHaveBeenLastCalledWith('saved', undefined)
  current.detach()
  await vi.advanceTimersByTimeAsync(0)
})
