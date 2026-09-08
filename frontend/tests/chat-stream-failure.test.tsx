import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ChatPane } from '@/components/chat/chat-pane'
import { TooltipProvider } from '@/components/ui/tooltip'
import { api, streamChat } from '@/lib/api'
import { SseStreamError, STREAM_CUT_MESSAGE } from '@/lib/sse'
import type { ChatEvent, MessageRead } from '@/types'

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  return {
    ...actual,
    api: {
      listSessions: vi.fn(),
      listMessages: vi.fn(),
      createSession: vi.fn(),
      listDocuments: vi.fn(),
      getSettings: vi.fn(),
      getClassProfile: vi.fn(),
      sendAgentChat: vi.fn(),
      retryAgentChat: vi.fn(),
      regenerateAgentChat: vi.fn(),
      stopAgentChat: vi.fn(),
      stopAgentChatStatus: vi.fn(),
    },
    streamChat: vi.fn(),
    streamRegenerate: vi.fn(),
  }
})

const QUESTION = 'Explain sum of two periodic signals'

function message(overrides: Partial<MessageRead> & { id: number }): MessageRead {
  return {
    role: 'user',
    content: '',
    thinking: '',
    thinking_ms: 0,
    retrieval_trimmed: false,
    omitted_document_count: 0,
    tool_activity: [],
    created_at: '2026-08-04T12:00:00Z',
    ...overrides,
  } as MessageRead
}

function renderPane() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <ChatPane
          classId={1}
          className="ECE 203"
          selectedDocumentId={null}
          sessionId={7}
          draft={false}
          onSessionIdChange={() => {}}
        />
      </TooltipProvider>
    </QueryClientProvider>,
  )
}

/**
 * Consumer regressions for the hardened transport (PLA-502): the new bounded failure
 * types (`SseStreamError`) must flow through the pane's existing recovery paths -
 * partial output stays, an accepted question is not restored, an unaccepted one is,
 * and the pane keeps accepting sends.
 */
describe('ChatPane stream-failure recovery (PLA-502)', () => {
  beforeEach(() => {
    vi.mocked(api.listSessions).mockResolvedValue([])
    vi.mocked(api.listDocuments).mockResolvedValue([])
    vi.mocked(api.getClassProfile).mockResolvedValue({
      facts: [],
      extraction_skipped_reason: null,
    })
    vi.mocked(api.getSettings).mockResolvedValue({
      endpoint_url: 'http://localhost:1234/v1',
      model: 'local',
    } as Awaited<ReturnType<typeof api.getSettings>>)
    vi.mocked(api.createSession).mockResolvedValue({
      id: 7,
      class_id: 1,
      title: null,
      mode: 'guide',
      artifact_part_id: null,
      created_at: '2026-08-04T12:00:00Z',
    } as Awaited<ReturnType<typeof api.createSession>>)
    vi.mocked(api.listMessages).mockResolvedValue([message({ id: 11, content: QUESTION })])
  })

  it('keeps the partial answer on screen and the newest typed draft when a turn fails mid-stream', async () => {
    // The request resolves neither now nor on its own: the turn streams first, and the
    // rejection lands later, while the student may already have started the next message.
    const stream: {
      emit?: (event: ChatEvent) => void
      accept?: () => void
      reject?: (error: Error) => void
    } = {}
    vi.mocked(streamChat).mockImplementation((_sessionId, _body, onEvent, _signal, onResponse) => {
      stream.emit = onEvent
      stream.accept = onResponse
      return new Promise<void>((_resolve, reject) => {
        stream.reject = reject
      })
    })

    const user = userEvent.setup()
    renderPane()
    const composer = await screen.findByLabelText('Message Lyra')
    await user.type(composer, QUESTION)
    await user.click(screen.getByLabelText('Send message'))

    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1))
    await act(async () => stream.accept?.())
    await act(async () => stream.emit?.({ type: 'start', message_id: 11 }))
    await act(async () => stream.emit?.({ type: 'token', text: 'Partial ' }))
    await act(async () => stream.emit?.({ type: 'token', text: 'answer' }))

    // The partial answer is on screen while the request is still pending. (The reveal
    // cascade splits it across elements, so assert on the answer container's text.)
    await waitFor(() => {
      const answer = document.querySelector('.assistant-content')
      expect(answer?.textContent).toBe('Partial answer')
    })

    // While the request is pending, the student starts the next message.
    await user.type(composer, ' follow-up draft')

    // Failure settlement reloads the durable transcript. The backend preserves a
    // partial ordinary reply, so model that readback rather than returning only the question.
    vi.mocked(api.listMessages).mockResolvedValue([
      message({ id: 11, content: QUESTION }),
      message({ id: 12, role: 'assistant', content: 'Partial answer' }),
    ])

    // The stream then fails mid-answer with the bounded transport error (PLA-502).
    await act(async () => stream.reject?.(new SseStreamError(STREAM_CUT_MESSAGE)))

    // The turn settles and the send button comes back (it is the Stop button mid-turn),
    // re-enabled by the newest typed draft: not wiped by the failure, not overwritten
    // by the already-accepted question.
    const send = await screen.findByLabelText('Send message')
    await waitFor(() => expect(send).toBeEnabled())
    expect(composer).toHaveValue(' follow-up draft')
    await waitFor(() => {
      expect(document.querySelector('.assistant-content')?.textContent).toBe('Partial answer')
    })

    // The pane keeps accepting sends: the typed draft goes out as the next turn.
    vi.mocked(streamChat).mockImplementation((_sessionId, _body, onEvent, _signal, onResponse) => {
      onResponse?.()
      onEvent({ type: 'token', text: 'Again' })
      onEvent({ type: 'done', message_id: 12 })
      return Promise.resolve()
    })
    await user.click(send)
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(composer).toHaveValue(''))
  })

  it('restores the typed draft when the stream fails before acceptance', async () => {
    // A failure the framing layer raises before the response is accepted is not an
    // ApiError - it must still take the pre-acceptance recovery path (draft restored).
    vi.mocked(streamChat).mockRejectedValue(new SseStreamError(STREAM_CUT_MESSAGE))

    const user = userEvent.setup()
    renderPane()
    const composer = await screen.findByLabelText('Message Lyra')
    await user.type(composer, QUESTION)
    await user.click(screen.getByLabelText('Send message'))

    await waitFor(() => expect(composer).toHaveValue(QUESTION))
  })
})
