import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { AgentChatError, api } from '@/lib/api'
import { agentKeys, useSendAgentChat } from '@/lib/hooks/use-agent'
import { chatKeys } from '@/lib/hooks/use-chat'
import { draftKeys } from '@/lib/hooks/use-drafts'
import { profileKeys } from '@/lib/hooks/use-profile'

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return { queryClient, wrapper }
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('useSendAgentChat', () => {
  it('invalidates chat, agent, workspace, profile, and source caches after success', async () => {
    vi.spyOn(api, 'sendAgentChat').mockResolvedValue({
      message_id: 44,
      content: 'Done.',
      stopped: 'complete',
      detail: 'Complete.',
      activity: [],
      source_ids: [],
      workspace_change_ids: [],
      command_request_ids: [],
      profile_fact_ids: [],
    })
    const { queryClient, wrapper } = createWrapper()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    const { result } = renderHook(() => useSendAgentChat(9, 12), { wrapper })
    result.current.mutate({ content: 'Inspect the parser', profile: 'code' })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(invalidate).toHaveBeenCalledWith({ queryKey: chatKeys.messages(12) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: chatKeys.sessions(9) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: agentKeys.workspace(9) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: agentKeys.activity(9, 12) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: agentKeys.changes(9, 12) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: agentKeys.commands(9, 12) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: profileKeys.forClass(9) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: draftKeys.sources(9) })
  })

  it('invalidates the same caches after a structured agent failure', async () => {
    vi.spyOn(api, 'sendAgentChat').mockRejectedValue(
      new AgentChatError(504, {
        detail: 'The tool loop timed out.',
        retryable: true,
        stopped: 'timeout',
        activity: [],
        source_ids: [17],
        workspace_change_ids: [4],
        command_request_ids: [9],
        profile_fact_ids: [12],
      }),
    )
    const { queryClient, wrapper } = createWrapper()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    const { result } = renderHook(() => useSendAgentChat(9, 12), { wrapper })
    result.current.mutate({ content: 'Inspect the parser', profile: 'research' })

    await waitFor(() => expect(result.current.isError).toBe(true))

    expect(invalidate).toHaveBeenCalledWith({ queryKey: chatKeys.messages(12) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: chatKeys.sessions(9) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: agentKeys.workspace(9) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: agentKeys.activity(9, 12) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: agentKeys.changes(9, 12) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: agentKeys.commands(9, 12) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: profileKeys.forClass(9) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: draftKeys.sources(9) })
  })
})
