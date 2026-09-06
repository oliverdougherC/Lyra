import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ChatPane } from '@/components/chat/chat-pane'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ApiError, api, streamChat, streamWriterChat } from '@/lib/api'
import type { ChatEvent } from '@/types'

vi.mock('@/components/chat/message-bubble', () => ({
  MessageRow: ({
    message,
    onRevealComplete,
  }: {
    message: { content: string }
    onRevealComplete?: () => void
  }) => (
    <div>
      {message.content}
      {onRevealComplete && <button onClick={onRevealComplete}>Finish reveal</button>}
    </div>
  ),
}))
vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  return {
    ...actual,
    api: {
      ...actual.api,
      listSessions: vi.fn(),
      listMessages: vi.fn(),
      listDocuments: vi.fn(),
      getSettings: vi.fn(),
      getClassProfile: vi.fn(),
      createSession: vi.fn(),
      createWriterSession: vi.fn(),
    },
    streamChat: vi.fn(),
    streamWriterChat: vi.fn(),
  }
})

const FOLLOW_UP = '  Next question\nwith exact spacing  '
function input() {
  return screen.getByLabelText('Message Lyra')
}
function edit(value: string) {
  fireEvent.change(input(), { target: { value } })
}
function enter() {
  fireEvent.keyDown(input(), { key: 'Enter' })
}

function mount(writer: boolean, sessionId: number | null = 7, onSessionIdChange = vi.fn()) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const pane = (id: number | null, classId = 1) => (
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <ChatPane
          classId={classId}
          selectedDocumentId={null}
          sessionId={id}
          onSessionIdChange={onSessionIdChange}
          draft={id === null}
          writer={writer ? { artifactId: 42 } : undefined}
          layout={writer ? 'inline' : 'pane'}
        />
      </TooltipProvider>
    </QueryClientProvider>
  )
  return { ...render(pane(sessionId)), pane }
}

function holdStream(writer: boolean) {
  let emit!: (event: ChatEvent) => void
  let finish!: () => void
  let fail!: (error: Error) => void
  const stream = writer ? vi.mocked(streamWriterChat) : vi.mocked(streamChat)
  const response = new Promise<void>((resolve, reject) => {
    finish = resolve
    fail = reject
  })
  if (writer) {
    vi.mocked(streamWriterChat).mockImplementation(
      (_artifact, _session, _body, onEvent, signal) => {
        emit = onEvent
        signal?.addEventListener('abort', () => fail(new DOMException('Stopped', 'AbortError')))
        return response
      },
    )
  } else {
    vi.mocked(streamChat).mockImplementation((_session, _body, onEvent, signal) => {
      emit = onEvent
      signal?.addEventListener('abort', () => fail(new DOMException('Stopped', 'AbortError')))
      return response
    })
  }
  return {
    stream,
    emit: (event: ChatEvent) => emit(event),
    finish: () => finish(),
    fail: (error: Error) => fail(error),
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.listSessions).mockResolvedValue([])
  vi.mocked(api.listMessages).mockResolvedValue([])
  vi.mocked(api.listDocuments).mockResolvedValue([])
  vi.mocked(api.getSettings).mockResolvedValue({
    endpoint_url: 'http://localhost:1234/v1',
  } as Awaited<ReturnType<typeof api.getSettings>>)
  vi.mocked(api.getClassProfile).mockResolvedValue({ facts: [], extraction_skipped_reason: null })
})

describe.each([false, true])('Follow-up drafting (embedded writer: %s)', (writer) => {
  it('keeps editing and focus through streaming and reveal, sending exactly once afterward', async () => {
    const turn = holdStream(writer)
    mount(writer)
    await waitFor(() => expect(input()).toBeEnabled())
    input().focus()
    edit('First question')
    enter()
    await waitFor(() => expect(turn.stream).toHaveBeenCalledTimes(1))
    expect(input()).toHaveFocus()
    expect(input()).toBeEnabled()
    edit(FOLLOW_UP)
    enter()
    expect(turn.stream).toHaveBeenCalledTimes(1)
    await act(async () => {
      turn.emit({ type: 'token', text: 'Answer' })
      turn.emit({ type: 'done', message_id: 11 })
      turn.finish()
    })
    expect(input()).toHaveValue(FOLLOW_UP)
    expect(screen.getByLabelText('Send message')).toBeDisabled()
    enter()
    expect(turn.stream).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByText('Finish reveal'))
    await waitFor(() => expect(screen.getByLabelText('Send message')).toBeEnabled())
    expect(input()).toHaveValue(FOLLOW_UP)
    enter()
    await waitFor(() => expect(turn.stream).toHaveBeenCalledTimes(2))
    const call = turn.stream.mock.calls[1]
    expect(call[writer ? 2 : 1]).toMatchObject({ content: FOLLOW_UP.trim() })
  })

  it.each(['failure', 'conflict', 'cancel'] as const)(
    'preserves the exact newer draft on %s',
    async (outcome) => {
      const turn = holdStream(writer)
      mount(writer)
      await waitFor(() => expect(input()).toBeEnabled())
      edit('Original question')
      enter()
      await waitFor(() => expect(turn.stream).toHaveBeenCalledTimes(1))
      edit(FOLLOW_UP)
      if (outcome === 'cancel') {
        fireEvent.click(screen.getByLabelText('Stop generating'))
      } else {
        await act(async () =>
          turn.fail(outcome === 'conflict' ? new ApiError(409, 'Busy') : new Error('Offline')),
        )
      }
      await waitFor(() => expect(screen.getByLabelText('Send message')).toBeEnabled())
      expect(input()).toHaveValue(FOLLOW_UP)
    },
  )

  it('does not restore the submitted question over a deliberately cleared follow-up', async () => {
    const turn = holdStream(writer)
    mount(writer)
    await waitFor(() => expect(input()).toBeEnabled())
    edit('Original question')
    enter()
    await waitFor(() => expect(turn.stream).toHaveBeenCalledTimes(1))
    edit('Changed my mind')
    edit('')
    await act(async () => turn.fail(new Error('Offline')))
    await waitFor(() => expect(screen.queryByLabelText('Stop generating')).not.toBeInTheDocument())
    expect(input()).toHaveValue('')
  })

  it('clears drafts on conversation/class navigation and ignores a late failure', async () => {
    const turn = holdStream(writer)
    const view = mount(writer)
    await waitFor(() => expect(input()).toBeEnabled())
    edit('Original question')
    enter()
    await waitFor(() => expect(turn.stream).toHaveBeenCalledTimes(1))
    edit(FOLLOW_UP)
    view.rerender(view.pane(8))
    await waitFor(() => expect(input()).toBeEnabled())
    expect(input()).toHaveValue('')
    edit('Other conversation')
    await act(async () => turn.fail(new Error('Late error')))
    expect(input()).toHaveValue('Other conversation')
    view.rerender(view.pane(8, 2))
    await waitFor(() => expect(input()).toBeEnabled())
    expect(input()).toHaveValue('')
  })
})

it('allows drafting while session creation is pending and guards repeated Enter sends', async () => {
  let reject!: (error: Error) => void
  vi.mocked(api.createSession).mockReturnValue(
    new Promise((_resolve, fail) => {
      reject = fail
    }),
  )
  mount(false, null)
  await waitFor(() => expect(input()).toBeEnabled())
  edit('  First question  ')
  enter()
  await waitFor(() => expect(api.createSession).toHaveBeenCalledTimes(1))
  edit(FOLLOW_UP)
  enter()
  expect(api.createSession).toHaveBeenCalledTimes(1)
  await act(async () => reject(new Error('Offline')))
  await waitFor(() => expect(screen.getByLabelText('Send message')).toBeEnabled())
  expect(input()).toHaveValue(FOLLOW_UP)
})

it('does not send into a class left while session creation was pending', async () => {
  let resolve!: (session: Awaited<ReturnType<typeof api.createSession>>) => void
  vi.mocked(api.createSession).mockReturnValue(
    new Promise((done) => {
      resolve = done
    }),
  )
  const view = mount(false, null)
  await waitFor(() => expect(input()).toBeEnabled())
  edit('Question for first class')
  enter()
  await waitFor(() => expect(api.createSession).toHaveBeenCalledTimes(1))
  view.rerender(view.pane(null, 2))
  await waitFor(() => expect(input()).toBeEnabled())
  edit('Question for another class')
  await act(async () =>
    resolve({ id: 9, mode: 'guide' } as Awaited<ReturnType<typeof api.createSession>>),
  )
  expect(streamChat).not.toHaveBeenCalled()
  expect(input()).toHaveValue('Question for another class')
  expect(screen.getByLabelText('Send message')).toBeEnabled()
})

it.each([false, true])(
  'revokes a pending session creation on unmount (writer: %s)',
  async (writer) => {
    let resolve!: (session: Awaited<ReturnType<typeof api.createSession>>) => void
    const creating = new Promise<Awaited<ReturnType<typeof api.createSession>>>((done) => {
      resolve = done
    })
    if (writer) vi.mocked(api.createWriterSession).mockReturnValue(creating)
    else vi.mocked(api.createSession).mockReturnValue(creating)
    const onSessionIdChange = vi.fn()
    const view = mount(writer, null, onSessionIdChange)
    await waitFor(() => expect(input()).toBeEnabled())
    edit('Question from the old pane')
    enter()
    await waitFor(() =>
      expect(writer ? api.createWriterSession : api.createSession).toHaveBeenCalledTimes(1),
    )
    view.unmount()
    await act(async () =>
      resolve({ id: 9, mode: 'guide' } as Awaited<ReturnType<typeof api.createSession>>),
    )
    expect(onSessionIdChange).not.toHaveBeenCalled()
    expect(streamChat).not.toHaveBeenCalled()
    expect(streamWriterChat).not.toHaveBeenCalled()
  },
)
