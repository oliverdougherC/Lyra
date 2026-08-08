import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/lib/api'
import { draftKeys, useCancelDraftRun } from '@/lib/hooks/use-drafts'
import type { DraftStatus } from '@/types'

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return { queryClient, wrapper }
}

const runningStatus: DraftStatus = {
  state: 'generating',
  stage_detail: 'Drafting 1.1 Introduction',
  error_message: null,
  problems_total: 4,
  problems_done: 1,
  run_id: 7,
  job_kind: 'pass',
  depth: 'standard',
  started_at: '2026-08-07T10:00:00Z',
  run_status: 'running',
  cancel_requested: false,
  cancel_requested_at: null,
  finished_at: null,
  warnings: [],
}

const cancellingStatus: DraftStatus = {
  ...runningStatus,
  stage_detail: 'Cancelling after the current step',
  run_status: 'cancel_requested',
  cancel_requested: true,
  cancel_requested_at: '2026-08-07T10:01:00Z',
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('useCancelDraftRun', () => {
  it('updates the cached status immediately and invalidates dependent draft queries', async () => {
    vi.spyOn(api, 'cancelDraftRun').mockResolvedValue(cancellingStatus)
    const { queryClient, wrapper } = createWrapper()
    queryClient.setQueryData(draftKeys.status(7), runningStatus)
    const setQueryData = vi.spyOn(queryClient, 'setQueryData')
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    const { result } = renderHook(() => useCancelDraftRun(7), { wrapper })
    result.current.mutate()

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(setQueryData).toHaveBeenCalledWith(draftKeys.status(7), cancellingStatus)
    expect(invalidate).toHaveBeenCalledWith({ queryKey: draftKeys.status(7) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: draftKeys.detail(7) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: draftKeys.pending(7) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: draftKeys.comments(7) })
    expect(queryClient.getQueryData(draftKeys.status(7))).toMatchObject({
      run_status: 'cancel_requested',
      cancel_requested: true,
    })
  })
})
