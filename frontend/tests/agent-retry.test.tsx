import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { MessageRow, type ChatMessage } from '@/components/chat/message-bubble'
import { TooltipProvider } from '@/components/ui/tooltip'
import { api } from '@/lib/api'
import { useRetryAgentChat } from '@/lib/hooks/use-agent'
import type { AgentAttempt } from '@/types'

function renderRow(msg: ChatMessage) {
  return render(
    <TooltipProvider>
      <MessageRow message={msg} />
    </TooltipProvider>,
  )
}

function message(overrides: Partial<ChatMessage>): ChatMessage {
  return {
    id: 1,
    role: 'user',
    content: 'Explain part b',
    thinking: '',
    thinking_ms: 0,
    retrieval_trimmed: false,
    omitted_document_count: 0,
    tool_activity: [],
    created_at: '2026-08-22T00:00:00Z',
    ...overrides,
  }
}

function attempt(state: AgentAttempt['state'], detail: string | null = null): AgentAttempt {
  return { state, stopped_reason: state, detail }
}

describe('the transcript shows a truthful failed agent turn (PLA-295)', () => {
  it('renders a failed turn as a failure under the question, with the bounded detail', () => {
    renderRow(
      message({ agent_attempt: attempt('failed', 'The tutor endpoint could not be reached.') }),
    )
    const failure = document.querySelector('[data-agent-turn-failure]')
    expect(failure).not.toBeNull()
    expect(failure).toHaveTextContent('The tutor endpoint could not be reached.')
  })

  it('renders a stopped turn as a failure too', () => {
    renderRow(message({ agent_attempt: attempt('stopped') }))
    expect(document.querySelector('[data-agent-turn-failure]')).not.toBeNull()
  })

  it('shows nothing extra for a completed turn or a plain tutor message', () => {
    renderRow(message({ agent_attempt: attempt('completed') }))
    expect(document.querySelector('[data-agent-turn-failure]')).toBeNull()
    cleanup()
    renderRow(message({ agent_attempt: null }))
    expect(document.querySelector('[data-agent-turn-failure]')).toBeNull()
  })
})

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return { queryClient, wrapper }
}

describe('useRetryAgentChat (PLA-295)', () => {
  it('retries the conversation, reusing the original message rather than sending a new one', async () => {
    const retry = vi.spyOn(api, 'retryAgentChat').mockResolvedValue({
      message_id: 5,
      content: 'Answered on the second try.',
      stopped: 'completed',
      detail: '',
      activity: [],
      source_ids: [],
      workspace_change_ids: [],
      command_request_ids: [],
      profile_fact_ids: [],
    })
    const send = vi.spyOn(api, 'sendAgentChat')
    const { wrapper } = createWrapper()

    const { result } = renderHook(() => useRetryAgentChat(7, 42), { wrapper })
    await result.current.mutateAsync()

    // Retry hits the retry endpoint with the class and session; it never sends a new message.
    expect(retry).toHaveBeenCalledWith(7, 42)
    expect(send).not.toHaveBeenCalled()
  })

  it('refuses to retry before a conversation exists', async () => {
    const retry = vi.spyOn(api, 'retryAgentChat')
    const { wrapper } = createWrapper()
    const { result } = renderHook(() => useRetryAgentChat(7, null), { wrapper })

    await expect(result.current.mutateAsync()).rejects.toThrow(/conversation/i)
    expect(retry).not.toHaveBeenCalled()
    await waitFor(() => expect(result.current.isError).toBe(true))
  })
})
