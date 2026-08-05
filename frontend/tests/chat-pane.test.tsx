import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ChatPane } from '@/components/chat/chat-pane'
import { TooltipProvider } from '@/components/ui/tooltip'
import { api, streamChat } from '@/lib/api'
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
    stream.emit?.({ type: 'token', text: 'A sum is periodic when…' })

    await waitFor(() => expect(screen.getAllByText(QUESTION)).toHaveLength(1))

    stream.emit?.({ type: 'done', message_id: 12 })
    stream.finish?.()

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
    stream.emit?.({ type: 'token', text: 'A sum is periodic when…' })
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
