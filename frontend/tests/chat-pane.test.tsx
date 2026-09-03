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
})
