import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ChatPane } from '@/components/chat/chat-pane'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ApiError, api, streamChat } from '@/lib/api'
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
        onClearSelectedDocument={() => {}}
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
            onClearSelectedDocument={() => {}}
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
            onClearSelectedDocument={() => {}}
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
 * boundary. The `start` frame is the server's acceptance signal; before it
 * arrives, a failure puts the text back in the composer. After it arrives, the
 * question is persisted and PLA-306's retry contract takes over.
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

  it('does not restore composer text after the start frame arrives (post-acceptance)', async () => {
    const stream: { emit?: (event: ChatEvent) => void; reject?: (error: Error) => void } = {}
    vi.mocked(streamChat).mockImplementation((_sessionId, _body, onEvent) => {
      stream.emit = onEvent
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
    await act(async () => stream.emit?.({ type: 'start', message_id: 11 }))
    await act(async () => stream.emit?.({ type: 'token', text: 'Partial' }))
    await act(async () => stream.reject?.(new Error('connection lost')))

    await waitFor(() => expect(composer).toHaveValue(''))
  })
})
