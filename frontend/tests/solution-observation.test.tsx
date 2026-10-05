import { focusManager, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { api } from '@/lib/api'
import { useSolutions, useSolutionStatus } from '@/lib/hooks/use-solutions'
import type { SolutionRead, SolutionStatus } from '@/types'

const solution: SolutionRead = {
  id: 5,
  class_id: 1,
  kind: 'solution_set',
  title: 'Practice',
  state: 'solving',
  stage_detail: null,
  problems_total: 1,
  problems_done: 0,
  error_message: null,
  created_at: '2026-10-05T00:00:00Z',
  updated_at: '2026-10-05T00:00:00Z',
  sources: [],
}
const status: SolutionStatus = {
  state: 'ready',
  stage_detail: null,
  problems_total: 1,
  problems_done: 1,
  error_message: null,
  parts: [],
}

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}
let client: QueryClient

beforeEach(() => {
  vi.useFakeTimers()
  focusManager.setFocused(true)
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  })
})

afterEach(() => {
  cleanup()
  client.clear()
  focusManager.setFocused(undefined)
  vi.useRealTimers()
})

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

it('updates running solutions on the work list and stops when they settle', async () => {
  const list = vi
    .spyOn(api, 'listSolutions')
    .mockResolvedValueOnce([solution])
    .mockResolvedValue([{ ...solution, state: 'ready', problems_done: 1 }])
  const { result } = renderHook(() => useSolutions(1), { wrapper })
  await advance(1)
  expect(result.current.data?.[0].state).toBe('solving')
  await advance(1600)
  expect(result.current.data?.[0].state).toBe('ready')
  expect(list).toHaveBeenCalledTimes(2)
  await advance(60000)
  expect(list).toHaveBeenCalledTimes(2)
})

it('backs failed status reads off to the recovery cadence', async () => {
  const read = vi.spyOn(api, 'getSolutionStatus').mockRejectedValue(new Error('Offline'))
  renderHook(() => useSolutionStatus(5), { wrapper })
  await advance(1)
  await advance(5000)
  expect(read).toHaveBeenCalledOnce()
  await advance(25000)
  expect(read).toHaveBeenCalledTimes(2)
})

it('reconciles a settled solution when the window returns to view', async () => {
  const read = vi
    .spyOn(api, 'getSolutionStatus')
    .mockResolvedValueOnce(status)
    .mockResolvedValue({ ...status, state: 'solving' })
  const { result } = renderHook(() => useSolutionStatus(5), { wrapper })
  await advance(1)
  act(() => focusManager.setFocused(false))
  await advance(6000)
  expect(read).toHaveBeenCalledOnce()
  act(() => focusManager.setFocused(true))
  await advance(1)
  expect(read).toHaveBeenCalledTimes(2)
  expect(result.current.data?.state).toBe('solving')
})
