/**
 * ChatPane-level integration: tutor retry visibility, dispatch, composer
 * preservation, and transcript refresh (PLA-306).
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ChatPane } from '@/components/chat/chat-pane'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ApiError, api, streamChatRetry, streamRegenerate } from '@/lib/api'
import type { ChatEvent, MessageRead, TutorAttempt } from '@/types'

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
    },
    streamChatRetry: vi.fn(),
    streamRegenerate: vi.fn(),
  }
})

const SESSION_ID = 10

function tutorMessage(
  overrides: Partial<MessageRead> & { id: number; tutor_attempt?: TutorAttempt | null },
): MessageRead {
  return {
    role: 'user',
    content: '',
    thinking: '',
    thinking_ms: 0,
    retrieval_trimmed: false,
    omitted_document_count: 0,
    tool_activity: [],
    created_at: '2026-08-27T12:00:00Z',
    session_id: SESSION_ID,
    ...overrides,
  } as MessageRead
}

function renderTutorPane() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <ChatPane
          classId={1}
          className="CS 101"
          selectedDocumentId={null}
          sessionId={SESSION_ID}
          draft={false}
          onSessionIdChange={() => {}}
        />
      </TooltipProvider>
    </QueryClientProvider>,
  )
}

describe('Tutor retry ChatPane integration', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.listSessions).mockResolvedValue([
      {
        id: SESSION_ID,
        class_id: 1,
        title: 'Tutor session',
        mode: 'guide',
        artifact_part_id: null,
        created_at: '2026-08-27T12:00:00Z',
      } as Awaited<ReturnType<typeof api.listSessions>>[0],
    ])
    vi.mocked(api.listDocuments).mockResolvedValue([])
    vi.mocked(api.getClassProfile).mockResolvedValue({
      facts: [],
      extraction_skipped_reason: null,
    })
    vi.mocked(api.getSettings).mockResolvedValue({
      endpoint_url: 'http://localhost:1234/v1',
      model: 'local',
    } as Awaited<ReturnType<typeof api.getSettings>>)
  })

  it('only the newest failed tutor user turn shows a Retry button', async () => {
    vi.mocked(api.listMessages).mockResolvedValue([
      tutorMessage({
        id: 1,
        role: 'user',
        content: 'Old question',
        tutor_attempt: { state: 'failed', stopped_reason: 'upstream_failed', detail: 'Error' },
      }),
      tutorMessage({
        id: 2,
        role: 'assistant',
        content: 'Old answer',
      }),
      tutorMessage({
        id: 3,
        role: 'user',
        content: 'New question',
        tutor_attempt: { state: 'failed', stopped_reason: 'upstream_failed', detail: 'Error' },
      }),
    ])

    renderTutorPane()
    await waitFor(() => expect(api.listMessages).toHaveBeenCalled())

    const retryButtons = await screen.findAllByRole('button', { name: /try again/i })
    expect(retryButtons).toHaveLength(1)
  })

  it('clicking Retry invokes streamChatRetry, not streamRegenerate', async () => {
    vi.mocked(api.listMessages).mockResolvedValue([
      tutorMessage({
        id: 1,
        role: 'user',
        content: 'My question',
        tutor_attempt: { state: 'failed', stopped_reason: 'upstream_failed', detail: 'Error' },
      }),
    ])

    vi.mocked(streamChatRetry).mockImplementation(
      async (_sid: number, _body: unknown, onEvent: (e: ChatEvent) => void) => {
        onEvent({ type: 'token', text: 'recovered' })
        onEvent({ type: 'done', message_id: 99 })
      },
    )

    const user = userEvent.setup()
    renderTutorPane()
    await waitFor(() => expect(api.listMessages).toHaveBeenCalled())

    const retryButton = await screen.findByRole('button', { name: /try again/i })
    await user.click(retryButton)

    await waitFor(() => expect(streamChatRetry).toHaveBeenCalled())
    expect(streamRegenerate).not.toHaveBeenCalled()
  })

  it('unsent composer text is preserved when retry gets a 409', async () => {
    vi.mocked(api.listMessages).mockResolvedValue([
      tutorMessage({
        id: 1,
        role: 'user',
        content: 'My question',
        tutor_attempt: { state: 'failed', stopped_reason: 'upstream_failed', detail: 'Error' },
      }),
    ])

    vi.mocked(streamChatRetry).mockRejectedValue(
      new ApiError(409, 'Another turn is still in progress on this conversation.'),
    )

    const user = userEvent.setup()
    renderTutorPane()
    await waitFor(() => expect(api.listMessages).toHaveBeenCalled())

    const composer = await screen.findByLabelText('Message Lyra')
    await user.type(composer, 'My unsent draft')

    const retryButton = await screen.findByRole('button', { name: /try again/i })
    await user.click(retryButton)

    await waitFor(() => expect(streamChatRetry).toHaveBeenCalled())

    await waitFor(() => {
      const input = screen.getByLabelText('Message Lyra')
      expect(input).toHaveValue('My unsent draft')
    })
  })

  it('successful retry refreshes the persisted transcript', async () => {
    vi.mocked(api.listMessages).mockResolvedValue([
      tutorMessage({
        id: 1,
        role: 'user',
        content: 'My question',
        tutor_attempt: { state: 'failed', stopped_reason: 'upstream_failed', detail: 'Error' },
      }),
    ])

    vi.mocked(streamChatRetry).mockImplementation(
      async (_sid: number, _body: unknown, onEvent: (e: ChatEvent) => void) => {
        onEvent({ type: 'token', text: 'Answer' })
        onEvent({ type: 'done', message_id: 99 })
      },
    )

    const user = userEvent.setup()
    renderTutorPane()
    await waitFor(() => expect(api.listMessages).toHaveBeenCalled())

    const callCountBefore = vi.mocked(api.listMessages).mock.calls.length

    const retryButton = await screen.findByRole('button', { name: /try again/i })
    await user.click(retryButton)
    await waitFor(() => expect(streamChatRetry).toHaveBeenCalled())

    await waitFor(() => {
      expect(vi.mocked(api.listMessages).mock.calls.length).toBeGreaterThan(callCountBefore)
    })
  })
})
