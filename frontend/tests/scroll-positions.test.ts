import { afterEach, expect, it, vi } from 'vitest'
import { createScrollPositions } from '@/router/scroll-positions'

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  sessionStorage.clear()
})

it('deduplicates unchanged snapshots and bounds checkpoints during sustained updates', () => {
  vi.useFakeTimers()
  const set = vi.spyOn(Storage.prototype, 'setItem')
  const positions = createScrollPositions()
  for (let tick = 0; tick < 1200; tick++) {
    positions.record('entry', { main: tick, files: tick * 2 })
    positions.record('entry', { main: tick, files: tick * 2 })
    vi.advanceTimersByTime(10)
  }
  expect(set).toHaveBeenCalledTimes(12)
  expect(positions.read('entry')).toEqual({ main: 1199, files: 2398 })
  for (let tick = 0; tick < 500; tick++) positions.record('entry', { main: 1199, files: 2398 })
  vi.advanceTimersByTime(5000)
  expect(set).toHaveBeenCalledTimes(12)
  positions.flush()
  expect(set).toHaveBeenCalledTimes(12)
})

it('flushes the trailing snapshot for reload and reads old entries beyond the memory cache', () => {
  const positions = createScrollPositions()
  for (let id = 0; id < 100; id++) {
    positions.record(String(id), { main: id + 10, files: id + 20 })
    positions.flush()
  }
  expect(positions.read('0')).toEqual({ main: 10, files: 20 })
  const reloaded = createScrollPositions()
  expect(reloaded.read('99')).toEqual({ main: 109, files: 119 })
})

it('retains memory positions when optional storage refuses reads or writes', () => {
  vi.useFakeTimers()
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
    throw new DOMException('Denied', 'SecurityError')
  })
  const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
    throw new DOMException('Full', 'QuotaExceededError')
  })
  const positions = createScrollPositions()
  positions.record('entry', { main: 42, files: 88 })
  vi.advanceTimersByTime(1000)
  expect(positions.read('entry')).toEqual({ main: 42, files: 88 })
  positions.record('entry', { main: 42, files: 88 })
  vi.advanceTimersByTime(10000)
  expect(set).toHaveBeenCalledOnce()
})
