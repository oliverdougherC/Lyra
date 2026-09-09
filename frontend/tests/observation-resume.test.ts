import { createElement, type ReactNode } from 'react'
import { QueryClient, QueryClientProvider, focusManager } from '@tanstack/react-query'
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { useDocuments } from '@/lib/hooks/use-documents'
import {
  agentKeys,
  beginAgentTurnObservation,
  endAgentTurnObservation,
  useAgentTurnsLive,
} from '@/lib/hooks/use-agent'

const originalVisibility = Object.getOwnPropertyDescriptor(document, 'visibilityState')
function visibility(value: string) {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value })
  document.dispatchEvent(new Event('visibilitychange', { bubbles: true }))
}
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.useRealTimers()
  if (originalVisibility) Object.defineProperty(document, 'visibilityState', originalVisibility)
  else delete (document as unknown as { visibilityState?: string }).visibilityState
  focusManager.setFocused(undefined)
})

it('reconciles a brief hide/resume even inside the production five-second stale window', async () => {
  vi.useFakeTimers()
  visibility('visible')
  const reads = vi
    .spyOn(api, 'listDocuments')
    .mockResolvedValue([{ id: 1, state: 'processing' }] as never)
  const client = new QueryClient({
    defaultOptions: { queries: { staleTime: 5000, retry: 1, refetchOnWindowFocus: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children)
  renderHook(() => useDocuments(1), { wrapper })
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1)
  })
  expect(reads).toHaveBeenCalledTimes(1)
  await act(async () => {
    visibility('hidden')
    await vi.advanceTimersByTimeAsync(100)
  })
  reads.mockResolvedValue([{ id: 1, state: 'ready' }] as never)
  await act(async () => {
    visibility('visible')
    await vi.advanceTimersByTimeAsync(1)
  })
  expect(reads).toHaveBeenCalledTimes(2)
  client.clear()
})

it('retains an owner announced during mount and collects the idle registry after unmount', async () => {
  vi.useFakeTimers()
  const client = new QueryClient({ defaultOptions: { queries: { gcTime: 50 } } })
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children)
  const view = renderHook(() => useAgentTurnsLive(1, 2), { wrapper })
  await act(async () => {
    beginAgentTurnObservation(client, 1, 2, 'turn-one')
    await vi.advanceTimersByTimeAsync(1)
  })
  expect(view.result.current).toBe(true)
  await act(async () => {
    endAgentTurnObservation(client, 1, 2, 'turn-one')
    await vi.advanceTimersByTimeAsync(1)
  })
  expect(view.result.current).toBe(false)
  view.unmount()
  await vi.advanceTimersByTimeAsync(51)
  expect(client.getQueryData(agentKeys.turns(1, 2))).toBeUndefined()
  client.clear()
})
