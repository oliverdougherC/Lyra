import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api } from '@/lib/api'
import {
  draftKeys,
  liveSuggestionPollInterval,
  useUpdateLiveDraftSuggestionBlock,
} from '@/lib/hooks/use-drafts'
import type { LiveDraftSuggestion } from '@/types'

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return { queryClient, wrapper }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

const LIVE_SUGGESTION: LiveDraftSuggestion = {
  id: 14,
  artifact_id: 3,
  run_id: 22,
  status: 'running',
  stage: 'drafting',
  stage_detail: 'Drafting 1.1 Introduction',
  version: 4,
  base_content: 'Base document.',
  blocks: [
    {
      id: 101,
      block_key: '1.1:intro',
      section_ref: '1.1',
      ordinal: 1,
      kind: 'paragraph',
      heading: 'Introduction',
      content: 'Original introduction.',
      status: 'drafted',
      target_words: 180,
      summary: 'Set up the thesis.',
      revision: 2,
      user_revision: 0,
    },
  ],
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('liveSuggestionPollInterval', () => {
  function query(status: LiveDraftSuggestion['status'] | undefined, dataUpdateCount = 0) {
    return {
      state: {
        data:
          status === undefined
            ? undefined
            : ({
                ...LIVE_SUGGESTION,
                status,
              } satisfies LiveDraftSuggestion),
        dataUpdateCount,
      },
    }
  }

  it('keeps polling before the suggestion exists and while it is still running', () => {
    expect(liveSuggestionPollInterval(query(undefined), true)).toBe(750)
    expect(liveSuggestionPollInterval(query('queued'), false)).toBe(750)
    expect(liveSuggestionPollInterval(query('running'), false)).toBeTypeOf('number')
  })

  it('stops once the live suggestion settles and no pass is running', () => {
    expect(liveSuggestionPollInterval(query('ready'), false)).toBe(false)
    expect(liveSuggestionPollInterval(query('failed'), false)).toBe(false)
    expect(liveSuggestionPollInterval(query('finalized'), false)).toBe(false)
    expect(liveSuggestionPollInterval(query('complete'), false)).toBe(false)
  })

  it('backs off to a two-second ceiling', () => {
    expect(liveSuggestionPollInterval(query('running', 0), false)).toBe(750)
    expect(liveSuggestionPollInterval(query('running', 2), false)).toBe(1250)
    expect(liveSuggestionPollInterval(query('running', 99), false)).toBe(2000)
  })
})

describe('useUpdateLiveDraftSuggestionBlock', () => {
  it('optimistically updates the block and then replaces it with the server copy', async () => {
    const pending = deferred<typeof LIVE_SUGGESTION.blocks[number]>()
    vi.spyOn(api, 'updateLiveDraftSuggestionBlock').mockImplementation(() => pending.promise)

    const { queryClient, wrapper } = createWrapper()
    queryClient.setQueryData(draftKeys.liveSuggestion(3), LIVE_SUGGESTION)

    const { result } = renderHook(() => useUpdateLiveDraftSuggestionBlock(3), { wrapper })

    result.current.mutate({
      blockId: 101,
      content: 'Edited introduction.',
      expectedRevision: 2,
      baseContent: 'Original introduction.',
    })

    await waitFor(() => {
      const optimistic = queryClient.getQueryData<LiveDraftSuggestion>(draftKeys.liveSuggestion(3))
      expect(optimistic?.blocks[0]).toMatchObject({
        content: 'Edited introduction.',
        revision: 3,
        user_revision: 1,
      })
    })

    pending.resolve({
      ...LIVE_SUGGESTION.blocks[0],
      content: 'Edited introduction.',
      revision: 7,
      user_revision: 3,
      status: 'revised',
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    const settled = queryClient.getQueryData<LiveDraftSuggestion>(draftKeys.liveSuggestion(3))
    expect(settled?.blocks[0]).toMatchObject({
      content: 'Edited introduction.',
      revision: 7,
      user_revision: 3,
      status: 'revised',
    })
    expect(api.updateLiveDraftSuggestionBlock).toHaveBeenCalledWith(3, 101, {
      content: 'Edited introduction.',
      expected_revision: 2,
      base_content: 'Original introduction.',
    })
  })

  it('rolls back and refetches when the server rejects the revision token', async () => {
    vi.spyOn(api, 'updateLiveDraftSuggestionBlock').mockRejectedValue(
      new ApiError(409, 'This block changed. Reload the live draft suggestion.'),
    )

    const { queryClient, wrapper } = createWrapper()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    queryClient.setQueryData(draftKeys.liveSuggestion(3), LIVE_SUGGESTION)

    const { result } = renderHook(() => useUpdateLiveDraftSuggestionBlock(3), { wrapper })

    result.current.mutate({
      blockId: 101,
      content: 'Edited introduction.',
      expectedRevision: 2,
      baseContent: 'Original introduction.',
    })

    await waitFor(() => expect(result.current.isError).toBe(true))

    const rolledBack = queryClient.getQueryData<LiveDraftSuggestion>(draftKeys.liveSuggestion(3))
    expect(rolledBack?.blocks[0]).toMatchObject({
      content: 'Original introduction.',
      revision: 2,
      user_revision: 0,
    })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: draftKeys.liveSuggestion(3) })
  })
})
