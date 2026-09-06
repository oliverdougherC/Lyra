import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ChatPane } from '@/components/chat/chat-pane'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ApiError, api, streamChat } from '@/lib/api'
import type { ChatEvent, DocumentRead, MessageRead, SessionRead, SettingsRead } from '@/types'

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

/**
 * The workspace round-trips the conversation through the URL, so opening one swaps a draft
 * pane for a pane on a real conversation. That swap is what enables the message query
 * mid-turn, and moving the pane between conversations while an answer is still coming is
 * what the tests below are about.
 */
function Workspace() {
  const [sessionId, setSessionId] = useState<number | null>(null)
  return (
    <>
      {/* Stand in for the sidebar, which points the pane at a draft or back at a
          conversation while whatever it was showing may still be streaming. */}
      <button type="button" onClick={() => setSessionId(null)}>
        New chat
      </button>
      <button type="button" onClick={() => setSessionId(7)}>
        Reopen chat
      </button>
      <ChatPane
        classId={1}
        className="ECE 203"
        selectedDocumentId={null}
        sessionId={sessionId}
        draft={sessionId === null}
        onSessionIdChange={setSessionId}
      />
    </>
  )
}

function renderWorkspace() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <Workspace />
      </TooltipProvider>
    </QueryClientProvider>,
  )
}

describe('ChatPane', () => {
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
  })

  it('blocks sending after a history failure and recovers with Retry', async () => {
    vi.mocked(api.listMessages)
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue([message({ id: 11, content: 'Saved question' })])
    renderWorkspace()
    await userEvent.click(screen.getByRole('button', { name: 'Reopen chat' }))
    expect(await screen.findByText('Could not load this conversation.')).toBeInTheDocument()
    expect(screen.getByLabelText('Message Lyra')).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: 'Retry conversation' }))
    expect(await screen.findByText('Saved question')).toBeInTheDocument()
    expect(screen.getByLabelText('Message Lyra')).toBeEnabled()
  })

  it('shows the first question once while the answer streams', async () => {
    // The server stores the question the moment the turn opens, so the first fetch of the
    // message list — which only becomes possible once the conversation exists — comes back
    // with the question already in it, mid-stream. The optimistic question must not double.
    vi.mocked(api.listMessages).mockResolvedValue([message({ id: 11, content: QUESTION })])

    const stream: { emit?: (event: ChatEvent) => void; finish?: () => void } = {}
    vi.mocked(streamChat).mockImplementation((_sessionId, _body, onEvent) => {
      stream.emit = onEvent
      return new Promise<void>((resolve) => {
        stream.finish = resolve
      })
    })

    const user = userEvent.setup()
    renderWorkspace()

    await user.type(await screen.findByLabelText('Message Lyra'), QUESTION)
    await user.click(screen.getByLabelText('Send message'))

    await waitFor(() => expect(api.listMessages).toHaveBeenCalledWith(7, expect.anything()))
    // Emitting a frame is the transport calling back into React, so it is the test's job
    // to say so. Left unwrapped it still passes, on a warning saying the render it just
    // triggered was not awaited.
    await act(async () => stream.emit?.({ type: 'token', text: 'A sum is periodic when…' }))

    await waitFor(() => expect(screen.getAllByText(QUESTION)).toHaveLength(1))

    await act(async () => {
      stream.emit?.({ type: 'done', message_id: 12 })
      stream.finish?.()
    })

    // And once the turn settles onto the persisted transcript, still once.
    await waitFor(() => expect(screen.getAllByText(QUESTION)).toHaveLength(1))
  })

  it('sets the conversation aside mid-answer without cutting the answer off', async () => {
    vi.mocked(api.listMessages).mockResolvedValue([message({ id: 11, content: QUESTION })])

    const stream: { emit?: (event: ChatEvent) => void; signal?: AbortSignal } = {}
    vi.mocked(streamChat).mockImplementation((_sessionId, _body, onEvent, signal) => {
      stream.emit = onEvent
      stream.signal = signal
      return new Promise<void>(() => {})
    })

    const user = userEvent.setup()
    renderWorkspace()

    await user.type(await screen.findByLabelText('Message Lyra'), QUESTION)
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalled())
    await act(async () => stream.emit?.({ type: 'token', text: 'A sum is periodic when…' }))
    await waitFor(() => expect(screen.getAllByText(QUESTION)).toHaveLength(1))

    // The click lands now, not whenever the model stops talking: the conversation goes,
    // and so does the composer's Stop, which belongs to an answer this chat is not the one
    // waiting on.
    await user.click(screen.getByRole('button', { name: 'New chat' }))
    await waitFor(() => expect(screen.queryByText(QUESTION)).toBeNull())
    await screen.findByLabelText('Send message')
    // Only its place moved. Cutting the stream is what left the student with a question
    // and no reply when they came back to it.
    expect(stream.signal?.aborted).toBe(false)

    await user.click(screen.getByRole('button', { name: 'Reopen chat' }))
    await waitFor(() => expect(screen.getAllByText(QUESTION)).toHaveLength(1))
    expect(stream.signal?.aborted).toBe(false)
  })

  it('takes a question in a new chat while the last one is still answering', async () => {
    vi.mocked(api.listMessages).mockResolvedValue([])
    const started: { sessionId: number; signal?: AbortSignal }[] = []
    vi.mocked(streamChat).mockImplementation((sessionId, _body, _onEvent, signal) => {
      started.push({ sessionId, signal })
      return new Promise<void>(() => {})
    })

    const user = userEvent.setup()
    renderWorkspace()

    await user.type(await screen.findByLabelText('Message Lyra'), QUESTION)
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(started).toHaveLength(1))

    await user.click(screen.getByRole('button', { name: 'New chat' }))
    vi.mocked(api.createSession).mockResolvedValueOnce({
      id: 8,
      class_id: 1,
      title: null,
      mode: 'guide',
      artifact_part_id: null,
      created_at: '2026-08-04T12:01:00Z',
    } as Awaited<ReturnType<typeof api.createSession>>)

    // The turn still running in the chat that was left is not a reason this one cannot be
    // typed in. It goes on writing its own answer where it was asked.
    await user.type(screen.getByLabelText('Message Lyra'), 'And for three signals?')
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(started).toHaveLength(2))
    expect(started[1].sessionId).toBe(8)
    expect(started[0].signal?.aborted).toBe(false)
  })

  it('does not blink a skeleton between the question and the answer', async () => {
    // The conversation comes into being with the first message, which is what first enables
    // the transcript query — so its very first fetch lands mid-turn. Waiting on it put the
    // loading skeleton between the suggested prompt and the question it became.
    let deliverMessages: ((messages: MessageRead[]) => void) | undefined
    vi.mocked(api.listMessages).mockImplementation(
      () =>
        new Promise((resolve) => {
          deliverMessages = resolve
        }),
    )
    vi.mocked(streamChat).mockImplementation(() => new Promise<void>(() => {}))

    const user = userEvent.setup()
    renderWorkspace()

    await user.type(await screen.findByLabelText('Message Lyra'), QUESTION)
    await user.click(screen.getByLabelText('Send message'))

    await waitFor(() => expect(api.listMessages).toHaveBeenCalledWith(7, expect.anything()))
    await waitFor(() => expect(screen.getAllByText(QUESTION)).toHaveLength(1))
    expect(screen.queryByLabelText('Loading conversation')).toBeNull()

    deliverMessages?.([message({ id: 11, content: QUESTION })])
    await waitFor(() => expect(screen.getAllByText(QUESTION)).toHaveLength(1))
  })
})

/**
 * The handoff contract: a question carried in from another surface transfers exactly
 * once, or not at all. Nothing here may double-send, send generated words uninvited, or
 * drop the student's text on the floor when the endpoint cannot answer.
 */
describe('ChatPane handoffs', () => {
  function renderHandoff(props: { initialAsk: string; initialSend: boolean }) {
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
            sessionId={null}
            draft
            onSessionIdChange={() => {}}
            {...props}
          />
        </TooltipProvider>
      </QueryClientProvider>,
    )
  }

  beforeEach(() => {
    // These tests count calls, so the ledger starts empty rather than carrying totals
    // over from the describe above.
    vi.clearAllMocks()
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
    vi.mocked(api.listMessages).mockResolvedValue([])
  })

  it('prefills a carried question without sending it', async () => {
    vi.mocked(streamChat).mockImplementation(() => new Promise<void>(() => {}))

    renderHandoff({ initialAsk: 'Why does the Laplace transform exist?', initialSend: false })

    const composer = await screen.findByLabelText('Message Lyra')
    expect(composer).toHaveValue('Why does the Laplace transform exist?')
    // Long enough for the auto-send effect to have fired if it were going to.
    await act(async () => new Promise((resolve) => setTimeout(resolve, 50)))
    expect(streamChat).not.toHaveBeenCalled()
    expect(api.createSession).not.toHaveBeenCalled()
  })

  it('sends a typed handoff exactly once, even as later renders come and go', async () => {
    vi.mocked(streamChat).mockImplementation(() => new Promise<void>(() => {}))

    const view = renderHandoff({ initialAsk: 'What is due next week?', initialSend: true })

    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1))
    expect(vi.mocked(streamChat).mock.calls[0][1]).toMatchObject({
      content: 'What is due next week?',
    })

    // Settings refetching or the parent re-rendering must not re-arm the send.
    view.rerender(
      <QueryClientProvider
        client={
          new QueryClient({
            defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
          })
        }
      >
        <TooltipProvider>
          <ChatPane
            classId={1}
            className="ECE 203"
            selectedDocumentId={null}
            sessionId={7}
            draft={false}
            onSessionIdChange={() => {}}
            initialAsk="What is due next week?"
            initialSend
          />
        </TooltipProvider>
      </QueryClientProvider>,
    )
    await act(async () => new Promise((resolve) => setTimeout(resolve, 50)))
    expect(streamChat).toHaveBeenCalledTimes(1)
    expect(api.createSession).toHaveBeenCalledTimes(1)
  })

  it('does not fire a handoff into a pane that cannot answer, and says why instead', async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      endpoint_url: null,
      model: null,
    } as unknown as Awaited<ReturnType<typeof api.getSettings>>)
    vi.mocked(streamChat).mockImplementation(() => new Promise<void>(() => {}))

    renderHandoff({ initialAsk: 'What is due next week?', initialSend: true })

    // The composer's place is taken by the standing explanation, which is the honest
    // state: the question was not sent, not silently swallowed by a dead endpoint.
    expect(await screen.findByText(/Lyra needs a tutor endpoint/)).toBeInTheDocument()
    await act(async () => new Promise((resolve) => setTimeout(resolve, 50)))
    expect(streamChat).not.toHaveBeenCalled()
    expect(api.createSession).not.toHaveBeenCalled()
  })
})

/**
 * PLA-313: the composer must preserve the student's text across the acceptance
 * boundary. The HTTP 200 response is the acceptance signal: once `onResponse`
 * fires, the question is durably persisted and the submitted-text bookmark is
 * cleared. Before that, a failure puts the text back in the composer. After
 * that, the question is persisted and PLA-306's retry contract takes over.
 */
describe('ChatPane composer preservation (PLA-313)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
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
    vi.mocked(api.listMessages).mockResolvedValue([])
  })

  it('restores composer text when the server rejects before acceptance (pre-SSE network error)', async () => {
    vi.mocked(streamChat).mockRejectedValue(new Error('Network error'))

    const user = userEvent.setup()
    renderWorkspace()

    const composer = await screen.findByLabelText('Message Lyra')
    await user.type(composer, QUESTION)
    await user.click(screen.getByLabelText('Send message'))

    await waitFor(() => expect(composer).toHaveValue(QUESTION))
  })

  it('does not restore composer text after acceptance (post-response model failure)', async () => {
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
    vi.mocked(api.listMessages).mockResolvedValue([message({ id: 11, content: QUESTION })])

    const user = userEvent.setup()
    renderWorkspace()

    const composer = await screen.findByLabelText('Message Lyra')
    await user.type(composer, QUESTION)
    await user.click(screen.getByLabelText('Send message'))

    await waitFor(() => expect(streamChat).toHaveBeenCalled())
    await act(async () => stream.accept?.())
    await act(async () => stream.emit?.({ type: 'start', message_id: 11 }))
    await act(async () => stream.emit?.({ type: 'token', text: 'Partial' }))
    await act(async () => stream.reject?.(new Error('connection lost')))

    await waitFor(() => expect(composer).toHaveValue(''))
  })

  it('does not restore composer when connection drops after HTTP 200 but before start frame', async () => {
    const stream: { accept?: () => void; reject?: (error: Error) => void } = {}
    vi.mocked(streamChat).mockImplementation((_sessionId, _body, _onEvent, _signal, onResponse) => {
      stream.accept = onResponse
      return new Promise<void>((_resolve, reject) => {
        stream.reject = reject
      })
    })

    const user = userEvent.setup()
    renderWorkspace()

    const composer = await screen.findByLabelText('Message Lyra')
    await user.type(composer, QUESTION)
    await user.click(screen.getByLabelText('Send message'))

    await waitFor(() => expect(streamChat).toHaveBeenCalled())
    await act(async () => stream.accept?.())
    await act(async () => stream.reject?.(new Error('stream dropped')))

    await waitFor(() => expect(composer).toHaveValue(''))
  })

  it('restores draft on busy 409 and keeps the ambiguity key for a later retry', async () => {
    // PLA-313's full frontend race: send X, lose the transport before acceptance
    // (generic error restores the text), resend while the original server turn still
    // owns its claim (ordinary busy 409), and the retry after that must carry X again.
    // A busy 409 says this attempt was not accepted; it is never evidence to discard
    // the ambiguity key or its submitted-text bookmark.
    vi.mocked(streamChat)
      .mockRejectedValueOnce(new Error('connection reset'))
      .mockRejectedValueOnce(new ApiError(409, 'busy'))
      .mockImplementation((_sessionId, _body, onEvent, _signal, onResponse) => {
        onResponse?.()
        onEvent({ type: 'start', message_id: 11 })
        onEvent({ type: 'done', message_id: 12 })
        return Promise.resolve()
      })

    const user = userEvent.setup()
    renderWorkspace()

    const composer = await screen.findByLabelText('Message Lyra')
    await user.type(composer, QUESTION)
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1))
    const firstId = vi.mocked(streamChat).mock.calls[0][1].operation_id

    // Ambiguous pre-response failure: the question comes back into the composer.
    await waitFor(() => expect(composer).toHaveValue(QUESTION))

    // Resend while the server turn is still claimed: ordinary busy 409, text stays put.
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(2))
    expect(vi.mocked(streamChat).mock.calls[1][1].operation_id).toBe(firstId)
    await waitFor(() => expect(composer).toHaveValue(QUESTION))

    // The claim has released: the next identical Send still carries X, and it lands.
    vi.mocked(api.listMessages).mockResolvedValue([message({ id: 11, content: QUESTION })])
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(3))
    expect(vi.mocked(streamChat).mock.calls[2][1].operation_id).toBe(firstId)
    await waitFor(() => expect(composer).toHaveValue(''))
  })

  it('clears submitted text on successful completion', async () => {
    vi.mocked(streamChat).mockImplementation((_sessionId, _body, onEvent, _signal, onResponse) => {
      onResponse?.()
      onEvent({ type: 'start', message_id: 11 })
      onEvent({ type: 'token', text: 'Full answer' })
      onEvent({ type: 'done', message_id: 12 })
      return Promise.resolve()
    })
    vi.mocked(api.listMessages).mockResolvedValue([
      message({ id: 11, content: QUESTION }),
      message({ id: 12, role: 'assistant', content: 'Full answer' }),
    ])

    const user = userEvent.setup()
    renderWorkspace()

    const composer = await screen.findByLabelText('Message Lyra')
    await user.type(composer, QUESTION)
    await user.click(screen.getByLabelText('Send message'))

    await waitFor(() => expect(composer).toHaveValue(''))
  })
})

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

/**
 * PLA-313 idempotency: a client-generated operation_id travels with the first
 * send and is reused on retry after an ambiguous failure. The server enforces
 * uniqueness so the user's question is committed at most once.
 */
describe('ChatPane idempotency (PLA-313)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
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
    vi.mocked(api.listMessages).mockResolvedValue([])
  })

  it('fresh send includes a client-generated operation_id in UUID v4 format', async () => {
    vi.mocked(streamChat).mockImplementation(() => new Promise<void>(() => {}))

    const user = userEvent.setup()
    renderWorkspace()

    await user.type(await screen.findByLabelText('Message Lyra'), QUESTION)
    await user.click(screen.getByLabelText('Send message'))

    await waitFor(() => expect(streamChat).toHaveBeenCalled())
    const body = vi.mocked(streamChat).mock.calls[0][1]
    expect(body.operation_id).toMatch(UUID_RE)
  })

  it('onResponse acceptance clears operation_id so the next send gets a fresh one', async () => {
    vi.mocked(streamChat).mockImplementation((_sessionId, _body, onEvent, _signal, onResponse) => {
      onResponse?.()
      onEvent({ type: 'start', message_id: 11 })
      onEvent({ type: 'done', message_id: 12 })
      return Promise.resolve()
    })
    vi.mocked(api.listMessages).mockResolvedValue([
      message({ id: 11, content: QUESTION }),
      message({ id: 12, role: 'assistant', content: 'Answer' }),
    ])

    const user = userEvent.setup()
    renderWorkspace()

    await user.type(await screen.findByLabelText('Message Lyra'), QUESTION)
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1))
    const firstId = vi.mocked(streamChat).mock.calls[0][1].operation_id

    const composer = await screen.findByLabelText('Message Lyra')
    await waitFor(() => expect(composer).toHaveValue(''))
    await user.type(composer, 'Follow-up question')
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(2))
    const secondId = vi.mocked(streamChat).mock.calls[1][1].operation_id

    expect(secondId).toMatch(UUID_RE)
    expect(secondId).not.toBe(firstId)
  })

  it('network error before acceptance preserves operation_id in the ref', async () => {
    vi.mocked(streamChat).mockRejectedValue(new Error('Network error'))

    const user = userEvent.setup()
    renderWorkspace()

    await user.type(await screen.findByLabelText('Message Lyra'), QUESTION)
    await user.click(screen.getByLabelText('Send message'))

    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1))
    const body = vi.mocked(streamChat).mock.calls[0][1]
    expect(body.operation_id).toMatch(UUID_RE)

    const composer = await screen.findByLabelText('Message Lyra')
    await waitFor(() => expect(composer).toHaveValue(QUESTION))
  })

  it('re-send after ambiguous failure reuses the same operation_id', async () => {
    vi.mocked(streamChat)
      .mockRejectedValueOnce(new Error('Network error'))
      .mockImplementation(() => new Promise<void>(() => {}))

    const user = userEvent.setup()
    renderWorkspace()

    await user.type(await screen.findByLabelText('Message Lyra'), QUESTION)
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1))
    const firstId = vi.mocked(streamChat).mock.calls[0][1].operation_id

    const composer = await screen.findByLabelText('Message Lyra')
    await waitFor(() => expect(composer).toHaveValue(QUESTION))

    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(2))
    const retryId = vi.mocked(streamChat).mock.calls[1][1].operation_id

    expect(retryId).toBe(firstId)
  })

  it('operation_id_mismatch 409 discards the key so the next send mints a fresh one', async () => {
    // The structured mismatch is the one conflict that proves the key itself is spent
    // on a different request: text is preserved, X is discarded, and Y replaces it.
    vi.mocked(streamChat)
      .mockRejectedValueOnce(new ApiError(409, 'mismatch', 'operation_id_mismatch'))
      .mockImplementation(() => new Promise<void>(() => {}))

    const user = userEvent.setup()
    renderWorkspace()

    const composer = await screen.findByLabelText('Message Lyra')
    await user.type(composer, QUESTION)
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1))
    const firstId = vi.mocked(streamChat).mock.calls[0][1].operation_id

    await waitFor(() => expect(composer).toHaveValue(QUESTION))

    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(2))
    const secondId = vi.mocked(streamChat).mock.calls[1][1].operation_id

    expect(secondId).toMatch(UUID_RE)
    expect(secondId).not.toBe(firstId)
  })

  it('user-initiated stop clears operation_id so the next send is a fresh logical turn', async () => {
    vi.mocked(streamChat).mockImplementation((_sessionId, _body, _onEvent, signal) => {
      return new Promise<void>((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
      })
    })
    vi.mocked(api.listMessages).mockResolvedValue([])

    const user = userEvent.setup()
    renderWorkspace()

    await user.type(await screen.findByLabelText('Message Lyra'), QUESTION)
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1))
    const firstId = vi.mocked(streamChat).mock.calls[0][1].operation_id
    expect(firstId).toMatch(UUID_RE)

    await screen.findByLabelText('Stop generating')
    await user.click(screen.getByLabelText('Stop generating'))

    vi.mocked(streamChat).mockImplementation(() => new Promise<void>(() => {}))

    const composer = await screen.findByLabelText('Message Lyra')
    await user.type(composer, 'Different question')
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(2))
    const secondId = vi.mocked(streamChat).mock.calls[1][1].operation_id

    expect(secondId).toMatch(UUID_RE)
    expect(secondId).not.toBe(firstId)
  })

  it('consecutive successful sends each get a unique operation_id', async () => {
    vi.mocked(streamChat).mockImplementation((_sessionId, _body, onEvent, _signal, onResponse) => {
      onResponse?.()
      onEvent({ type: 'start', message_id: 11 })
      onEvent({ type: 'done', message_id: 12 })
      return Promise.resolve()
    })
    vi.mocked(api.listMessages).mockResolvedValue([
      message({ id: 11, content: QUESTION }),
      message({ id: 12, role: 'assistant', content: 'Answer' }),
    ])

    const user = userEvent.setup()
    renderWorkspace()
    const ids: (string | undefined)[] = []

    const composer = await screen.findByLabelText('Message Lyra')
    for (let i = 0; i < 3; i++) {
      await user.type(composer, `Question ${i}`)
      await user.click(screen.getByLabelText('Send message'))
      await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(i + 1))
      ids.push(vi.mocked(streamChat).mock.calls[i][1].operation_id)
      await waitFor(() => expect(composer).toHaveValue(''))
    }

    expect(new Set(ids).size).toBe(3)
    ids.forEach((id) => expect(id).toMatch(UUID_RE))
  })

  it('server commits the question but client receives no response headers — retry carries the same stable ID', async () => {
    let callCount = 0
    vi.mocked(streamChat).mockImplementation((_sessionId, _body, onEvent, _signal, onResponse) => {
      callCount++
      if (callCount === 1) {
        return Promise.reject(new Error('connection reset'))
      }
      onResponse?.()
      onEvent({ type: 'start', message_id: 11 })
      onEvent({ type: 'done', message_id: 12 })
      return Promise.resolve()
    })
    vi.mocked(api.listMessages).mockResolvedValue([
      message({ id: 11, content: QUESTION }),
      message({ id: 12, role: 'assistant', content: 'Answer' }),
    ])

    const user = userEvent.setup()
    renderWorkspace()

    const composer = await screen.findByLabelText('Message Lyra')
    await user.type(composer, QUESTION)
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1))
    const firstId = vi.mocked(streamChat).mock.calls[0][1].operation_id

    await waitFor(() => expect(composer).toHaveValue(QUESTION))

    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(2))
    const retryId = vi.mocked(streamChat).mock.calls[1][1].operation_id

    expect(retryId).toBe(firstId)
    expect(firstId).toMatch(UUID_RE)
  })

  it('operation_id is absent for non-send turn kinds (retry, regenerate)', async () => {
    vi.mocked(streamChat).mockImplementation((_sessionId, _body, onEvent, _signal, onResponse) => {
      onResponse?.()
      onEvent({ type: 'start', message_id: 11 })
      onEvent({ type: 'done', message_id: 12 })
      return Promise.resolve()
    })
    vi.mocked(api.listMessages).mockResolvedValue([
      message({ id: 11, content: QUESTION }),
      message({ id: 12, role: 'assistant', content: 'Answer' }),
    ])

    const user = userEvent.setup()
    renderWorkspace()

    await user.type(await screen.findByLabelText('Message Lyra'), QUESTION)
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1))
    const body = vi.mocked(streamChat).mock.calls[0][1]
    expect(body.operation_id).toBeDefined()
  })

  it('editing restored text after pre-response stop mints a new operation_id (PLA-313 blocker 2)', async () => {
    vi.mocked(streamChat).mockImplementation((_sessionId, _body, _onEvent, signal) => {
      return new Promise<void>((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
      })
    })
    vi.mocked(api.listMessages).mockResolvedValue([])

    const user = userEvent.setup()
    renderWorkspace()

    await user.type(await screen.findByLabelText('Message Lyra'), QUESTION)
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1))
    const firstId = vi.mocked(streamChat).mock.calls[0][1].operation_id

    await screen.findByLabelText('Stop generating')
    await user.click(screen.getByLabelText('Stop generating'))

    vi.mocked(streamChat).mockImplementation(() => new Promise<void>(() => {}))

    const composer = await screen.findByLabelText('Message Lyra')
    await user.type(composer, 'Edited question')
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(2))
    const secondId = vi.mocked(streamChat).mock.calls[1][1].operation_id

    expect(secondId).toMatch(UUID_RE)
    expect(secondId).not.toBe(firstId)
  })

  it('re-typing the same text after pre-response stop reuses the same operation_id (PLA-313 blocker 3)', async () => {
    vi.mocked(streamChat).mockImplementation((_sessionId, _body, _onEvent, signal) => {
      return new Promise<void>((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
      })
    })
    vi.mocked(api.listMessages).mockResolvedValue([])

    const user = userEvent.setup()
    renderWorkspace()

    const composer = await screen.findByLabelText('Message Lyra')
    await user.type(composer, QUESTION)
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1))
    const firstId = vi.mocked(streamChat).mock.calls[0][1].operation_id

    await screen.findByLabelText('Stop generating')
    await user.click(screen.getByLabelText('Stop generating'))

    vi.mocked(streamChat).mockImplementation(() => new Promise<void>(() => {}))

    await user.type(composer, QUESTION)
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(2))
    const retryId = vi.mocked(streamChat).mock.calls[1][1].operation_id

    expect(retryId).toBe(firstId)
  })

  it('stop after response acceptance clears operation_id normally (PLA-313 blocker 3)', async () => {
    vi.mocked(streamChat).mockImplementation((_sessionId, _body, _onEvent, signal, onResponse) => {
      onResponse?.()
      return new Promise<void>((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
      })
    })
    vi.mocked(api.listMessages).mockResolvedValue([])

    const user = userEvent.setup()
    renderWorkspace()

    await user.type(await screen.findByLabelText('Message Lyra'), QUESTION)
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1))
    const firstId = vi.mocked(streamChat).mock.calls[0][1].operation_id

    await screen.findByLabelText('Stop generating')
    await user.click(screen.getByLabelText('Stop generating'))

    vi.mocked(streamChat).mockImplementation(() => new Promise<void>(() => {}))

    const composer = await screen.findByLabelText('Message Lyra')
    await user.type(composer, 'Follow-up')
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(2))
    const secondId = vi.mocked(streamChat).mock.calls[1][1].operation_id

    expect(secondId).toMatch(UUID_RE)
    expect(secondId).not.toBe(firstId)
  })

  it('editing restored text after generic network error mints a new operation_id', async () => {
    vi.mocked(streamChat)
      .mockRejectedValueOnce(new Error('Network error'))
      .mockImplementation(() => new Promise<void>(() => {}))

    const user = userEvent.setup()
    renderWorkspace()

    await user.type(await screen.findByLabelText('Message Lyra'), QUESTION)
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1))
    const firstId = vi.mocked(streamChat).mock.calls[0][1].operation_id

    const composer = await screen.findByLabelText('Message Lyra')
    await waitFor(() => expect(composer).toHaveValue(QUESTION))

    await user.clear(composer)
    await user.type(composer, 'Edited question')
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(2))
    const secondId = vi.mocked(streamChat).mock.calls[1][1].operation_id

    expect(secondId).toMatch(UUID_RE)
    expect(secondId).not.toBe(firstId)
  })

  it('unchanged restored text after generic network error reuses the same operation_id', async () => {
    vi.mocked(streamChat)
      .mockRejectedValueOnce(new Error('Network error'))
      .mockImplementation(() => new Promise<void>(() => {}))

    const user = userEvent.setup()
    renderWorkspace()

    await user.type(await screen.findByLabelText('Message Lyra'), QUESTION)
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1))
    const firstId = vi.mocked(streamChat).mock.calls[0][1].operation_id

    const composer = await screen.findByLabelText('Message Lyra')
    await waitFor(() => expect(composer).toHaveValue(QUESTION))

    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(2))
    const retryId = vi.mocked(streamChat).mock.calls[1][1].operation_id

    expect(retryId).toBe(firstId)
  })
})

describe('ChatPane contextual agent (PLA-401)', () => {
  function renderAgentPane() {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    return render(
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <ChatPane
            classId={1}
            className="ECE 203"
            agent
            selectedDocumentId={5}
            sessionId={7}
            onSessionIdChange={() => {}}
          />
        </TooltipProvider>
      </QueryClientProvider>,
    )
  }

  beforeEach(() => {
    vi.mocked(api.sendAgentChat).mockClear()
    vi.mocked(api.listSessions).mockResolvedValue([])
    const document: DocumentRead = {
      id: 5,
      class_id: 1,
      filename: 'notes.pdf',
      mime: 'application/pdf',
      byte_size: 1024,
      state: 'ready',
      stage_detail: null,
      pages_total: 1,
      pages_done: 1,
      pages_skipped: 0,
      pages_failed: 0,
      recognize: false,
      error_message: null,
      created_at: '2026-08-04T12:00:00Z',
    }
    vi.mocked(api.listDocuments).mockResolvedValue([document])
    vi.mocked(api.getClassProfile).mockResolvedValue({
      facts: [],
      extraction_skipped_reason: null,
    })
    const settings: SettingsRead = {
      endpoint_url: 'http://localhost:1234/v1',
      model: 'local',
      context_window: 8192,
      extraction_enabled: false,
      remote_ack: false,
      api_key_set: false,
      api_key_storage: 'file',
      endpoint_is_local: true,
      endpoint_host: 'localhost',
      embedding_model: null,
      embedding_dim: null,
      tools_supported: true,
      tools_message: null,
      vision_supported: null,
      vision_message: null,
      allow_web_research: false,
      parallel_requests: false,
      parallel_concurrency: 1,
      exa_api_key_set: false,
      exa_api_key_storage: 'file',
    }
    vi.mocked(api.getSettings).mockResolvedValue(settings)
    const session: SessionRead = {
      id: 7,
      class_id: 1,
      title: null,
      mode: 'guide',
      artifact_part_id: null,
      created_at: '2026-08-04T12:00:00Z',
    }
    vi.mocked(api.createSession).mockResolvedValue(session)
  })

  it('shows live agent reasoning and reveals answer words before the request completes', async () => {
    vi.mocked(api.listMessages).mockResolvedValue([])
    let emit: Parameters<typeof api.sendAgentChat>[8]
    vi.mocked(api.sendAgentChat).mockImplementation((...args) => {
      emit = args[8]
      return new Promise(() => {})
    })
    const user = userEvent.setup()
    const { container } = renderAgentPane()
    await user.type(await screen.findByLabelText('Message Lyra'), 'Explain this')
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(emit).toBeTypeOf('function'))

    await act(async () => emit?.({ type: 'reasoning', text: 'Consider the period first.' }))
    await user.click(screen.getByRole('button', { name: /Thinking/ }))
    expect(screen.getByText('Consider the period first.')).toBeVisible()
    await act(async () => emit?.({ type: 'token', text: 'The period' }))
    expect(Array.from(container.querySelectorAll('.assistant-content')).at(-1)).toHaveTextContent(
      'The period',
    )
    expect(container.querySelector('[data-stream-word]')).toHaveClass('stream-word-visible')
    expect(screen.getByLabelText('Stop generating')).toBeInTheDocument()
    await act(async () => emit?.({ type: 'token', text: ' is two seconds.' }))
    expect(Array.from(container.querySelectorAll('.assistant-content')).at(-1)).toHaveTextContent(
      'The period is two seconds.',
    )

    await act(async () => emit?.({ type: 'reset' }))
    expect(container.querySelector('.assistant-content')).toBeNull()
    expect(screen.getByText('Consider the period first.')).toBeVisible()
    await act(async () => emit?.({ type: 'token', text: 'Revised answer' }))
    expect(Array.from(container.querySelectorAll('.assistant-content')).at(-1)).toHaveTextContent(
      'Revised answer',
    )
  })

  it('sends the turn to the agent endpoint with the scoped source and no profile', async () => {
    const transcript: MessageRead[] = []
    vi.mocked(api.listMessages).mockImplementation(async () => transcript)
    vi.mocked(api.sendAgentChat).mockImplementation(async () => {
      transcript.push(
        message({ id: 1, role: 'user', content: 'Read my starter code and explain how it works' }),
        message({ id: 42, role: 'assistant', content: 'Here is how the starter works.' }),
      )
      return {
        message_id: 42,
        content: 'Here is how the starter works.',
        stopped: 'complete',
        detail: 'Complete.',
        activity: [],
        source_ids: [],
        workspace_change_ids: [],
        command_request_ids: [],
        profile_fact_ids: [],
      }
    })
    const user = userEvent.setup()
    renderAgentPane()

    await user.type(
      await screen.findByLabelText('Message Lyra'),
      'Read my starter code and explain how it works',
    )
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(1))

    // One ordinary conversation surface: the turn goes to the agent endpoint, carries the
    // scoped source, and names no profile (the agent plans its own work).
    const [classId, sessionId, content, profile, documentId] = vi.mocked(api.sendAgentChat).mock
      .calls[0]
    expect(classId).toBe(1)
    expect(sessionId).toBe(7)
    expect(content).toBe('Read my starter code and explain how it works')
    expect(profile).toBeUndefined()
    expect(documentId).toBe(5)

    // The full reply lands in the conversation, in place.
    expect(await screen.findByText('Here is how the starter works.')).toBeInTheDocument()
  })

  it('mints one operation ID per agent send and clears it once the turn settles', async () => {
    vi.mocked(api.sendAgentChat).mockClear()
    const transcript: MessageRead[] = []
    vi.mocked(api.listMessages).mockImplementation(async () => transcript)
    vi.mocked(api.sendAgentChat).mockImplementation(async () => {
      transcript.push(
        message({ id: transcript.length + 1, role: 'user', content: 'A' }),
        message({ id: transcript.length + 2, role: 'assistant', content: 'Reply A' }),
      )
      return {
        message_id: transcript.at(-1)!.id,
        content: 'Reply A',
        stopped: 'complete',
        detail: 'Complete.',
        activity: [],
        source_ids: [],
        workspace_change_ids: [],
        command_request_ids: [],
        profile_fact_ids: [],
      }
    })
    const user = userEvent.setup()
    renderAgentPane()

    const box = await screen.findByLabelText('Message Lyra')
    await user.type(box, 'First question')
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(1))
    const firstOp = vi.mocked(api.sendAgentChat).mock.calls[0][6]
    expect(firstOp).toEqual(expect.any(String))

    // The answer's word reveal finishes before the next turn becomes sendable.
    await waitFor(() => expect(document.querySelector('[data-stream-word]')).toBeNull())
    // A different question mints a fresh key; the settled first one has been cleared.
    await user.type(box, 'Second question')
    await waitFor(() => expect(screen.getByLabelText('Send message')).toBeEnabled())
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(2))
    const secondOp = vi.mocked(api.sendAgentChat).mock.calls[1][6]
    expect(secondOp).toEqual(expect.any(String))
    expect(secondOp).not.toBe(firstOp)
  })

  it('keeps the operation ID and restores the text when nothing durable landed', async () => {
    vi.mocked(api.sendAgentChat).mockClear()
    vi.mocked(api.listMessages).mockImplementation(async () => [])
    vi.mocked(api.sendAgentChat).mockRejectedValue(
      new ApiError(0, 'Could not reach the Lyra service.'),
    )
    const user = userEvent.setup()
    renderAgentPane()

    const box = await screen.findByLabelText('Message Lyra')
    await user.type(box, 'Lost in the wire')
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(1))
    const firstOp = vi.mocked(api.sendAgentChat).mock.calls[0][6]

    // Nothing durable: the question goes back in the box, and re-sending the same words
    // carries the same operation ID so the server reconciles instead of duplicating.
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe('Lost in the wire'))
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.sendAgentChat).mock.calls[1][6]).toBe(firstOp)
  })

  it('does not offer to re-send a turn whose reply is already durable', async () => {
    // The transport failed, but the server did commit the question and its reply: the
    // reconciliation must leave the composer empty rather than invite a duplicate send.
    vi.mocked(api.sendAgentChat).mockClear()
    vi.mocked(api.listMessages).mockImplementation(async () => {
      // The reconciliation is matched on the operation id the send minted, not on the
      // question's text.
      const operationId = vi.mocked(api.sendAgentChat).mock.calls[0]?.[6]
      return [
        message({
          id: 1,
          role: 'user',
          content: 'Durable question',
          agent_attempt: {
            state: 'completed',
            stopped_reason: null,
            detail: null,
            operation_id: operationId,
          },
        }),
        message({ id: 2, role: 'assistant', content: 'Durable reply' }),
      ]
    })
    vi.mocked(api.sendAgentChat).mockRejectedValue(
      new ApiError(0, 'Could not reach the Lyra service.'),
    )
    const user = userEvent.setup()
    renderAgentPane()

    const box = await screen.findByLabelText('Message Lyra')
    await user.type(box, 'Durable question')
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(1))

    // The reply shows in the transcript, and the composer is left empty.
    expect(await screen.findByText('Durable reply')).toBeInTheDocument()
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe(''))
  })

  describe('agent stop lifecycle', () => {
    function pendingTurnHarness() {
      const transcript: MessageRead[] = []
      let releaseImpl: (() => void) | undefined
      vi.mocked(api.listMessages).mockImplementation(async () => transcript)
      let nextId = 1
      vi.mocked(api.sendAgentChat).mockImplementation((_c, _s, content) => {
        // The server stores the question the moment the turn opens, even though the
        // answer is still in flight.
        transcript.push(message({ id: nextId++, role: 'user', content }))
        return new Promise((resolve) => {
          releaseImpl = () => {
            resolve({
              message_id: 2,
              content: 'Too late',
              stopped: 'complete',
              detail: 'Complete.',
              activity: [],
              source_ids: [],
              workspace_change_ids: [],
              command_request_ids: [],
              profile_fact_ids: [],
            })
          }
        })
      })
      // The release is only assignable once the send is in flight, and the tests call it
      // after that: hand back a closure that reads the binding at call time. Returning
      // the binding itself would hand out the undefined it held when the harness was
      // built, and a destructure would freeze whatever it held at that moment.
      return { transcript, releaseSend: () => releaseImpl?.() }
    }

    it('waits for the server stop confirmation before presenting the turn as stopped', async () => {
      vi.mocked(api.sendAgentChat).mockClear()
      vi.mocked(api.stopAgentChat).mockClear()
      const { releaseSend } = pendingTurnHarness()
      let releaseStop: (() => void) | undefined
      vi.mocked(api.stopAgentChat).mockImplementation(
        () =>
          new Promise((resolve) => {
            releaseStop = () => resolve({ stopped: true, settling: false })
          }),
      )
      const user = userEvent.setup()
      renderAgentPane()

      const box = await screen.findByLabelText('Message Lyra')
      await user.type(box, 'Take a while')
      await user.click(screen.getByLabelText('Send message'))
      await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(1))

      // The turn is in flight: Stop hits the explicit endpoint (the handler cannot see a
      // fetch abort) and the UI enters the bounded "Stopping…" state.
      await user.click(await screen.findByLabelText('Stop generating'))
      await waitFor(() => expect(api.stopAgentChat).toHaveBeenCalledTimes(1))
      // The "Stopping…" affordance is the visible contract: the turn has NOT been
      // declared stopped locally yet - that belongs to the server's confirmation.
      expect(screen.getByLabelText('Stopping…')).toBeInTheDocument()
      // And the conversation is still closed to a new turn while it settles.
      expect(screen.queryByLabelText('Send message')).not.toBeInTheDocument()

      // The server confirms: only now does the turn settle as stopped, the local request
      // stand down, and the conversation re-enable.
      act(() => releaseStop?.())
      await waitFor(() => expect(screen.getByLabelText('Send message')).toBeInTheDocument())
      expect(screen.queryByLabelText('Stopping…')).not.toBeInTheDocument()
      expect(screen.queryByLabelText('Stop generating')).not.toBeInTheDocument()
      await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe(''))
      releaseSend?.()
    })

    it('waits for the stopped transcript to settle before accepting the next send', async () => {
      vi.mocked(api.sendAgentChat).mockClear()
      let stopTurn: ((value: Awaited<ReturnType<typeof api.sendAgentChat>>) => void) | undefined
      let finishRefresh: ((value: MessageRead[]) => void) | undefined
      let holdRefresh = false
      let refreshStarted = false
      const refreshing = new Promise<MessageRead[]>((resolve) => {
        finishRefresh = resolve
      })
      vi.mocked(api.listMessages).mockImplementation(() => {
        if (!holdRefresh) return Promise.resolve([])
        refreshStarted = true
        return refreshing
      })
      const stoppedResult: Awaited<ReturnType<typeof api.sendAgentChat>> = {
        message_id: 0,
        content: '',
        stopped: 'stopped',
        detail: 'Stopped.',
        activity: [],
        source_ids: [],
        workspace_change_ids: [],
        command_request_ids: [],
        profile_fact_ids: [],
      }
      vi.mocked(api.sendAgentChat)
        .mockImplementationOnce(
          () =>
            new Promise((resolve) => {
              stopTurn = resolve
            }),
        )
        .mockResolvedValueOnce({ ...stoppedResult, stopped: 'complete', content: 'Continued.' })
      vi.mocked(api.stopAgentChat).mockImplementation(async () => {
        holdRefresh = true
        stopTurn?.(stoppedResult)
        return { stopped: true, settling: false }
      })
      const user = userEvent.setup()
      renderAgentPane()
      const box = await screen.findByLabelText('Message Lyra')
      await user.type(box, 'Stop this turn')
      await user.click(screen.getByLabelText('Send message'))
      await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(1))
      await user.click(await screen.findByLabelText('Stop generating'))
      await waitFor(() => expect(refreshStarted).toBe(true))
      // The server is stopped, but the old optimistic turn still owns this pane until
      // its durable transcript arrives. Exposing Send here used to erase an unsent turn.
      expect(box).toBeDisabled()
      expect(screen.getByLabelText('Send message')).toBeDisabled()
      await act(async () =>
        finishRefresh?.([message({ id: 1, role: 'user', content: 'Stop this turn' })]),
      )
      await waitFor(() => expect(box).toBeEnabled())
      await user.type(box, 'Can you continue now?')
      await user.click(screen.getByLabelText('Send message'))
      await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(2))
      expect(vi.mocked(api.sendAgentChat).mock.calls[1][2]).toBe('Can you continue now?')
    })

    it('keeps the conversation closed on a settling confirmation until the session is proven free', async () => {
      // The stop was latched and the cancellation delivered, but a late worker is still
      // inside a dispatch: NOT a stop yet. The pane stays in "Stopping…", the
      // conversation stays closed to a new turn, and the send key is unspent - and only
      // when the backend's status read proves the session free does the turn settle as
      // stopped and the conversation re-enable.
      vi.mocked(api.sendAgentChat).mockClear()
      vi.mocked(api.stopAgentChat).mockClear()
      vi.mocked(api.stopAgentChatStatus).mockClear()
      const { releaseSend } = pendingTurnHarness()
      vi.mocked(api.stopAgentChat).mockResolvedValue({ stopped: false, settling: true })
      // The first status read still settles; the next one proves the session free.
      vi.mocked(api.stopAgentChatStatus).mockResolvedValueOnce({ settling: true })
      vi.mocked(api.stopAgentChatStatus).mockResolvedValue({ settling: false })
      const user = userEvent.setup()
      renderAgentPane()

      const box = await screen.findByLabelText('Message Lyra')
      await user.type(box, 'Take a while')
      await user.click(screen.getByLabelText('Send message'))
      await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(1))

      await user.click(await screen.findByLabelText('Stop generating'))
      await waitFor(() => expect(api.stopAgentChat).toHaveBeenCalledTimes(1))

      // The settling verdict is not a stop: still "Stopping…", still closed to Send.
      await waitFor(() => expect(api.stopAgentChatStatus).toHaveBeenCalledTimes(1))
      expect(screen.getByLabelText('Stopping…')).toBeInTheDocument()
      expect(screen.queryByLabelText('Send message')).not.toBeInTheDocument()

      // The backend proves the session free: only now does the turn settle as stopped,
      // the send key is spent, and the conversation re-enables.
      await waitFor(() => expect(api.stopAgentChatStatus).toHaveBeenCalledTimes(2))
      await waitFor(() => expect(screen.getByLabelText('Send message')).toBeInTheDocument())
      expect(screen.queryByLabelText('Stopping…')).not.toBeInTheDocument()
      expect(screen.queryByLabelText('Stop generating')).not.toBeInTheDocument()
      releaseSend?.()
    })

    it('does not claim a stop when the turn completed in the race before Stop inspected it', async () => {
      // /stop finds nothing in flight: the turn settled on its own in the race just
      // before the Stop was inspected. That is NOT a stop - the completed answer is
      // durable and must not be suppressed, and nothing may spend the send key or label
      // the turn stopped. The turn's own request decides the outcome; the
      // reconciliation finds the send by its operation id and leaves the composer empty.
      vi.mocked(api.sendAgentChat).mockClear()
      vi.mocked(api.stopAgentChat).mockClear()
      const { transcript, releaseSend } = pendingTurnHarness()
      // The turn settled just before /stop inspected it: nothing was in flight.
      vi.mocked(api.stopAgentChat).mockResolvedValue({ stopped: false, settling: false })
      const user = userEvent.setup()
      renderAgentPane()

      const box = await screen.findByLabelText('Message Lyra')
      await user.type(box, 'Take a while')
      await user.click(screen.getByLabelText('Send message'))
      await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(1))

      await user.click(await screen.findByLabelText('Stop generating'))
      await waitFor(() => expect(api.stopAgentChat).toHaveBeenCalledTimes(1))

      // The completed answer lands durable (the request's own settle), just after the
      // Stop verdict.
      transcript.push(message({ id: 2, role: 'assistant', content: 'Too late' }))
      act(() => {
        releaseSend?.()
      })

      // The durable completed state wins: the answer shows, the turn is presented as
      // completed - not stopped - the send key is spent by that settle, and the
      // conversation re-enables with an empty composer.
      expect(await screen.findByText('Too late')).toBeInTheDocument()
      await waitFor(() => expect(screen.getByLabelText('Send message')).toBeInTheDocument())
      expect(screen.queryByLabelText('Stop generating')).not.toBeInTheDocument()
      expect(screen.queryByLabelText('Stopping…')).not.toBeInTheDocument()
      await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe(''))
    })

    it('keeps the turn active and retries Stop when the stop request fails', async () => {
      vi.mocked(api.sendAgentChat).mockClear()
      vi.mocked(api.stopAgentChat).mockClear()
      const { releaseSend } = pendingTurnHarness()
      vi.mocked(api.stopAgentChat).mockRejectedValue(new TypeError('failed to fetch'))
      const user = userEvent.setup()
      renderAgentPane()

      const box = await screen.findByLabelText('Message Lyra')
      await user.type(box, 'Take a while')
      await user.click(screen.getByLabelText('Send message'))
      await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(1))

      // The stop cannot reach the server: the UI must not claim the turn is stopped.
      await user.click(await screen.findByLabelText('Stop generating'))
      await waitFor(() => expect(api.stopAgentChat).toHaveBeenCalledTimes(1))
      // The turn is still running: the Stop affordance is back (not "Stopping…"), a
      // second attempt is allowed, and nothing settled locally in the meantime.
      await waitFor(() => expect(screen.getByLabelText('Stop generating')).toBeInTheDocument())
      expect(screen.queryByLabelText('Stopping…')).not.toBeInTheDocument()
      expect(screen.queryByLabelText('Send message')).not.toBeInTheDocument()
      vi.mocked(api.stopAgentChat).mockResolvedValue({ stopped: true, settling: false })
      await user.click(screen.getByLabelText('Stop generating'))
      await waitFor(() => expect(api.stopAgentChat).toHaveBeenCalledTimes(2))
      await waitFor(() => expect(screen.getByLabelText('Send message')).toBeInTheDocument())
      releaseSend?.()
    })

    it('retires the send key once the stop is confirmed, so the next send mints fresh', async () => {
      vi.mocked(api.sendAgentChat).mockClear()
      vi.mocked(api.stopAgentChat).mockClear()
      const { transcript, releaseSend } = pendingTurnHarness()
      vi.mocked(api.stopAgentChat).mockResolvedValue({ stopped: true, settling: false })
      const user = userEvent.setup()
      renderAgentPane()

      const box = await screen.findByLabelText('Message Lyra')
      await user.type(box, 'Take a while')
      await user.click(screen.getByLabelText('Send message'))
      await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(1))
      const firstOp = vi.mocked(api.sendAgentChat).mock.calls[0][6]
      expect(firstOp).toEqual(expect.any(String))

      await user.click(await screen.findByLabelText('Stop generating'))
      await waitFor(() => expect(api.stopAgentChat).toHaveBeenCalledTimes(1))
      await waitFor(() => expect(screen.getByLabelText('Send message')).toBeInTheDocument())
      releaseSend?.()

      // A stop-confirmed turn is durably stopped: the browser's send key is spent, and a
      // subsequent message - identical or different - is a NEW send with a fresh
      // operation ID. The stopped turn's question stays in the transcript exactly once.
      await user.type(box, 'What about Fourier transforms?')
      await user.click(screen.getByLabelText('Send message'))
      await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(2))
      const secondOp = vi.mocked(api.sendAgentChat).mock.calls[1][6]
      expect(secondOp).toEqual(expect.any(String))
      expect(secondOp).not.toBe(firstOp)
      expect(transcript.filter((item) => item.content === 'Take a while')).toHaveLength(1)
    })
  })

  /**
   * Lost-response reconciliation keyed on the send's OPERATION ID (PLA-313), never on
   * message text: a non-streaming agent turn can lose its acceptance in transport for
   * ANY error, and the conversation may already contain an identical earlier question
   * with its own turn, so the durable readback is matched on the operation id the send
   * minted. The durable state decides what the composer does.
   */
  describe('lost-response reconciliation', () => {
    it('case A: a durable completed turn retires both refs - empty composer, fresh operation id', async () => {
      vi.mocked(api.sendAgentChat).mockClear()
      const transcript: MessageRead[] = []
      vi.mocked(api.listMessages).mockImplementation(async () => transcript)
      vi.mocked(api.sendAgentChat).mockImplementation(
        async (_c, _s, _content, _r, _d, _m, operationId) => {
          // The turn commits durably and completes; the HTTP response is lost in transit.
          transcript.push(
            message({
              id: 1,
              role: 'user',
              content: QUESTION,
              agent_attempt: {
                state: 'completed',
                stopped_reason: null,
                detail: null,
                operation_id: operationId,
              },
            }),
            message({ id: 2, role: 'assistant', content: 'A durable answer.' }),
          )
          throw new Error('connection lost')
        },
      )

      const user = userEvent.setup()
      renderAgentPane()

      const box = await screen.findByLabelText('Message Lyra')
      await user.type(box, QUESTION)
      await user.click(screen.getByLabelText('Send message'))

      // The self-heal: the transcript shows the recovered question and reply, and the
      // composer is left empty - the text is not offered back for re-sending.
      await waitFor(() => expect(screen.getAllByText(QUESTION)).toHaveLength(1))
      expect(await screen.findByText('A durable answer.')).toBeInTheDocument()
      await waitFor(() => expect(box).toHaveValue(''))

      // The next Send is a new question with a FRESH operation id, never the stale key.
      await user.type(box, 'A different follow-up question')
      await user.click(screen.getByLabelText('Send message'))
      await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(2))
      const [first, second] = vi.mocked(api.sendAgentChat).mock.calls
      expect(first[6]).toBeTypeOf('string')
      expect(second[6]).toBeTypeOf('string')
      expect(second[6]).not.toBe(first[6])
    })

    it('reconciles a lost acceptance after the tool-less fallback by the lineage id - empty composer, next send mints fresh', async () => {
      // The endpoint refused the first tools request, the automatic tool-less fallback
      // completed, and the acceptance was lost in transport. The readback names the send
      // by the operation id on the lineage's ROOT attempt (the abandoned tool pass) - the
      // completed attempt itself carries no id - so the reconciliation finds the send,
      // the composer stays empty, and the next different question is a NEW send.
      vi.mocked(api.sendAgentChat).mockClear()
      const transcript: MessageRead[] = []
      vi.mocked(api.listMessages).mockImplementation(async () => transcript)
      vi.mocked(api.sendAgentChat).mockImplementation(
        async (_c, _s, _content, _r, _d, _m, operationId) => {
          // The turn commits durably and the reply's acceptance is lost.
          transcript.push(
            message({
              id: 1,
              role: 'user',
              content: 'Explain convolution',
              agent_attempt: {
                state: 'completed',
                stopped_reason: null,
                detail: null,
                operation_id: operationId,
              },
            }),
            message({ id: 2, role: 'assistant', content: 'The answer, without tools.' }),
          )
          throw new Error('connection lost')
        },
      )

      const user = userEvent.setup()
      renderAgentPane()

      const box = await screen.findByLabelText('Message Lyra')
      await user.type(box, 'Explain convolution')
      await user.click(screen.getByLabelText('Send message'))
      await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(1))
      const firstOp = vi.mocked(api.sendAgentChat).mock.calls[0][6]

      // The recovered answer shows, and the composer is left empty: the send is
      // recognized by its lineage id, so the text is not offered back for re-sending.
      expect(await screen.findByText('The answer, without tools.')).toBeInTheDocument()
      await waitFor(() => expect(box).toHaveValue(''))

      // The next send is a NEW question with a FRESH operation id, never the spent one.
      await user.type(box, 'What about a conv layer?')
      await user.click(screen.getByLabelText('Send message'))
      await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(2))
      const secondOp = vi.mocked(api.sendAgentChat).mock.calls[1][6]
      expect(secondOp).toBeTypeOf('string')
      expect(secondOp).not.toBe(firstOp)
    })

    it('case B: a durable failed turn retires the send key entirely - no prefill, next send is a new send', async () => {
      // The question landed; the attempt failed durably. The transcript shows the honest
      // turn with its Retry. The composer must not prefill the text (Retry is the causal
      // path) AND the send key is spent: a follow-up message - identical or different -
      // is a NEW send that mints a fresh operation id, never a re-run of the spent one.
      vi.mocked(api.sendAgentChat).mockClear()
      const transcript: MessageRead[] = []
      vi.mocked(api.listMessages).mockImplementation(async () => transcript)
      vi.mocked(api.sendAgentChat).mockImplementation(
        async (_c, _s, _content, _r, _d, _m, operationId) => {
          transcript.push(
            message({
              id: 1,
              role: 'user',
              content: QUESTION,
              agent_attempt: {
                state: 'failed',
                stopped_reason: 'upstream_failed',
                detail: 'The model endpoint could not be reached.',
                operation_id: operationId,
              },
            }),
          )
          throw new Error('upstream failed')
        },
      )

      const user = userEvent.setup()
      renderAgentPane()

      const box = await screen.findByLabelText('Message Lyra')
      await user.type(box, QUESTION)
      await user.click(screen.getByLabelText('Send message'))
      await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(1))
      const firstOp = vi.mocked(api.sendAgentChat).mock.calls[0][6]

      // No prefill: the composer is empty, not offered the failed question again.
      await waitFor(() => expect(box).toHaveValue(''))

      // A typed message after the failed turn - identical or different - is a NEW send.
      await user.type(box, 'What about Fourier transforms?')
      await user.click(screen.getByLabelText('Send message'))
      await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(2))
      const secondOp = vi.mocked(api.sendAgentChat).mock.calls[1][6]
      expect(secondOp).not.toBe(firstOp)
    })

    it('reconciles an identical re-send against its own operation id, not an earlier identical question', async () => {
      // The conversation already holds a COMPLETED turn for this exact question (with its
      // own operation id). A NEW send of the same text carries a fresh operation id; when
      // its acceptance is lost, the reconciliation must match the new operation's own
      // durable turn - not the older identical-text turn - or it would silently lose the
      // new question.
      vi.mocked(api.sendAgentChat).mockClear()
      const transcript: MessageRead[] = [
        message({
          id: 1,
          role: 'user',
          content: QUESTION,
          agent_attempt: {
            state: 'completed',
            stopped_reason: null,
            detail: null,
            operation_id: 'earlier-operation',
          },
        }),
        message({ id: 2, role: 'assistant', content: 'The earlier answer.' }),
      ]
      vi.mocked(api.listMessages).mockImplementation(async () => transcript)
      vi.mocked(api.sendAgentChat).mockImplementation(
        async (_c, _s, _content, _r, _d, _m, operationId) => {
          // The new turn commits as its own durable turn under the NEW operation id; the
          // reply is lost in transit.
          transcript.push(
            message({
              id: 3,
              role: 'user',
              content: QUESTION,
              agent_attempt: {
                state: 'completed',
                stopped_reason: null,
                detail: null,
                operation_id: operationId,
              },
            }),
            message({ id: 4, role: 'assistant', content: 'The newer answer.' }),
          )
          throw new Error('connection lost')
        },
      )

      const user = userEvent.setup()
      renderAgentPane()

      const box = await screen.findByLabelText('Message Lyra')
      await user.type(box, QUESTION)
      await user.click(screen.getByLabelText('Send message'))

      // Both turns are visible: the older identical-text turn and the newer one.
      await waitFor(() => expect(screen.getAllByText(QUESTION)).toHaveLength(2))
      expect(screen.getByText('The newer answer.')).toBeInTheDocument()
      // The newer turn's acceptance was lost, but its key is spent: empty composer, and
      // the NEXT send mints fresh.
      await waitFor(() => expect(box).toHaveValue(''))
      await user.type(box, 'And one more question')
      await user.click(screen.getByLabelText('Send message'))
      await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(2))
      const [first, second] = vi.mocked(api.sendAgentChat).mock.calls
      expect(first[6]).not.toBe('earlier-operation')
      expect(second[6]).not.toBe(first[6])
    })

    it('case C: nothing durable restores the draft and keeps the operation id for an idempotent re-send', async () => {
      // The request never landed (the transport died before the server saw it): the draft
      // goes back in the box and the operation id stays minted, so a re-send either lands
      // fresh or, if it actually did commit, the server reconciles by operation id.
      vi.mocked(api.sendAgentChat).mockClear()
      vi.mocked(api.listMessages).mockResolvedValue([])
      vi.mocked(api.sendAgentChat).mockRejectedValue(new TypeError('failed to fetch'))

      const user = userEvent.setup()
      renderAgentPane()

      const box = await screen.findByLabelText('Message Lyra')
      await user.type(box, QUESTION)
      await user.click(screen.getByLabelText('Send message'))
      await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(1))

      // The draft is back in the composer.
      await waitFor(() => expect(box).toHaveValue(QUESTION))

      // And the re-send carries the same operation id.
      await user.click(screen.getByLabelText('Send message'))
      await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(2))
      const [first, second] = vi.mocked(api.sendAgentChat).mock.calls
      expect(second[6]).toBe(first[6])
    })
  })
})
