/**
 * Composer state survives writer-retry 409 errors and successful retries.
 *
 * PLA-310: a student who types a new message in the composer, then clicks Retry
 * on a failed writer turn, must keep their typed text regardless of whether
 * the retry succeeds, fails with a concurrent-turn 409, or is refused with
 * `writer_retry_has_effects`.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ChatPane } from '@/components/chat/chat-pane'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ApiError, api, streamWriterChat, streamWriterChatRetry } from '@/lib/api'
import type { ChatEvent, MessageRead, WriterAttempt } from '@/types'

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
    streamWriterChat: vi.fn(),
    streamWriterChatRetry: vi.fn(),
  }
})

const DRAFT_ID = 42
const SESSION_ID = 7

function writerMessage(
  overrides: Partial<MessageRead> & { id: number; writer_attempt?: WriterAttempt | null },
): MessageRead {
  return {
    role: 'user',
    content: '',
    thinking: '',
    thinking_ms: 0,
    retrieval_trimmed: false,
    omitted_document_count: 0,
    tool_activity: [],
    created_at: '2026-08-26T12:00:00Z',
    session_id: SESSION_ID,
    ...overrides,
  } as MessageRead
}

function renderWriterPane() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <ChatPane
          classId={1}
          className="ENG 101"
          selectedDocumentId={null}
          onClearSelectedDocument={() => {}}
          writer={{ artifactId: DRAFT_ID }}
          sessionId={SESSION_ID}
          draft={false}
          onSessionIdChange={() => {}}
        />
      </TooltipProvider>
    </QueryClientProvider>,
  )
}

describe('Writer retry composer preservation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.listSessions).mockResolvedValue([
      {
        id: SESSION_ID,
        class_id: 1,
        title: 'Draft conversation',
        mode: 'writer',
        artifact_part_id: 1,
        created_at: '2026-08-26T12:00:00Z',
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

  it('unsent composer text survives concurrent-turn writer retry 409', async () => {
    // Set up a failed writer turn in the transcript
    vi.mocked(api.listMessages).mockResolvedValue([
      writerMessage({
        id: 11,
        role: 'user',
        content: 'Original question',
        writer_attempt: { state: 'failed', stopped_reason: 'upstream_failed', detail: 'Error' },
      }),
    ])

    // The retry will fail with a concurrent-turn 409
    vi.mocked(streamWriterChatRetry).mockRejectedValue(
      new ApiError(409, 'Another turn is still in progress on this conversation.'),
    )

    const user = userEvent.setup()
    renderWriterPane()

    // Wait for the transcript to load
    await waitFor(() => expect(api.listMessages).toHaveBeenCalled())

    // Type something new in the composer
    const composer = await screen.findByLabelText('Message Lyra')
    await user.type(composer, 'My new unsent message')

    // Click the retry button
    const retryButton = await screen.findByRole('button', { name: /try again/i })
    await user.click(retryButton)

    // Wait for the 409 to be handled
    await waitFor(() => expect(streamWriterChatRetry).toHaveBeenCalled())

    // The composer must still contain the student's unsent text
    await waitFor(() => {
      const input = screen.getByLabelText('Message Lyra')
      expect(input).toHaveValue('My new unsent message')
    })
  })

  it('unsent composer text survives writer_retry_has_effects 409', async () => {
    vi.mocked(api.listMessages).mockResolvedValue([
      writerMessage({
        id: 11,
        role: 'user',
        content: 'Original question',
        writer_attempt: { state: 'failed', stopped_reason: 'upstream_failed', detail: 'Error' },
      }),
    ])

    vi.mocked(streamWriterChatRetry).mockRejectedValue(
      new ApiError(
        409,
        'The previous attempt made changes before it failed.',
        'writer_retry_has_effects',
      ),
    )

    const user = userEvent.setup()
    renderWriterPane()
    await waitFor(() => expect(api.listMessages).toHaveBeenCalled())

    const composer = await screen.findByLabelText('Message Lyra')
    await user.type(composer, 'Another unsent message')

    const retryButton = await screen.findByRole('button', { name: /try again/i })
    await user.click(retryButton)
    await waitFor(() => expect(streamWriterChatRetry).toHaveBeenCalled())

    await waitFor(() => {
      const input = screen.getByLabelText('Message Lyra')
      expect(input).toHaveValue('Another unsent message')
    })
  })

  it('ordinary writer SEND that gets a 409 restores the attempted prompt', async () => {
    vi.mocked(api.listMessages).mockResolvedValue([])

    vi.mocked(streamWriterChat).mockRejectedValue(
      new ApiError(409, 'Another turn is still in progress on this conversation.'),
    )

    const user = userEvent.setup()
    renderWriterPane()
    await waitFor(() => expect(api.listMessages).toHaveBeenCalled())

    const composer = await screen.findByLabelText('Message Lyra')
    await user.type(composer, 'My important question')
    await user.click(screen.getByLabelText('Send message'))

    await waitFor(() => expect(streamWriterChat).toHaveBeenCalled())

    // For a normal send, the composer should restore the attempted prompt
    await waitFor(() => {
      const input = screen.getByLabelText('Message Lyra')
      expect(input).toHaveValue('My important question')
    })
  })

  it('successful retry does not clear text typed independently in the composer', async () => {
    vi.mocked(api.listMessages).mockResolvedValue([
      writerMessage({
        id: 11,
        role: 'user',
        content: 'Original question',
        writer_attempt: { state: 'failed', stopped_reason: 'upstream_failed', detail: 'Error' },
      }),
    ])

    const stream: { emit?: (event: ChatEvent) => void; finish?: () => void } = {}
    vi.mocked(streamWriterChatRetry).mockImplementation((_draftId, _sessionId, onEvent) => {
      stream.emit = onEvent
      return new Promise<void>((resolve) => {
        stream.finish = resolve
      })
    })

    const user = userEvent.setup()
    renderWriterPane()
    await waitFor(() => expect(api.listMessages).toHaveBeenCalled())

    // Type something in the composer before clicking retry
    const composer = await screen.findByLabelText('Message Lyra')
    await user.type(composer, 'Next question')

    // Click retry
    const retryButton = await screen.findByRole('button', { name: /try again/i })
    await user.click(retryButton)
    await waitFor(() => expect(streamWriterChatRetry).toHaveBeenCalled())

    // Complete the retry successfully
    await act(async () => {
      stream.emit?.({ type: 'token', text: 'Here is the answer.' })
      stream.emit?.({ type: 'done', message_id: 12 })
      stream.finish?.()
    })

    // The composer should still have the independently typed text
    await waitFor(() => {
      const input = screen.getByLabelText('Message Lyra')
      expect(input).toHaveValue('Next question')
    })
  })

  it('repeated retry clicks while one retry is active do not disturb composer state', async () => {
    vi.mocked(api.listMessages).mockResolvedValue([
      writerMessage({
        id: 11,
        role: 'user',
        content: 'Original question',
        writer_attempt: { state: 'failed', stopped_reason: 'upstream_failed', detail: 'Error' },
      }),
    ])

    // First retry starts streaming
    vi.mocked(streamWriterChatRetry).mockImplementation(() => new Promise<void>(() => {}))

    const user = userEvent.setup()
    renderWriterPane()
    await waitFor(() => expect(api.listMessages).toHaveBeenCalled())

    const composer = await screen.findByLabelText('Message Lyra')
    await user.type(composer, 'Preserved text')

    const retryButton = await screen.findByRole('button', { name: /try again/i })
    await user.click(retryButton)
    await waitFor(() => expect(streamWriterChatRetry).toHaveBeenCalledTimes(1))

    // The pendingTurn guard prevents a second call, so the composer is untouched
    expect(screen.getByLabelText('Message Lyra')).toHaveValue('Preserved text')
    expect(streamWriterChatRetry).toHaveBeenCalledTimes(1)
  })
})
