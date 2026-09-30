import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'

import { ChatPane } from '@/components/chat/chat-pane'
import { SourceContext } from '@/components/chat/source-context'
import { TooltipProvider } from '@/components/ui/tooltip'
import { api, type AgentStreamEvent } from '@/lib/api'
import type { DocumentRead, MessageRead, SettingsRead } from '@/types'

const nativeSurface = vi.hoisted(() => ({ active: false, occluded: false }))
const scrollNativeToBottom = vi.hoisted(() => () => Promise.resolve())

vi.mock('@/components/chat/native-chat-host', () => ({
  useNativeChatHost: (
    _enabled: boolean,
    _snapshot: unknown,
    _callbacks: unknown,
    _ref: unknown,
    occluded: boolean,
  ) => {
    nativeSurface.occluded = occluded
    return {
      hostRef: { current: null },
      active: nativeSurface.active && !occluded,
      scrollToBottom: scrollNativeToBottom,
    }
  },
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
      sendAgentChat: vi.fn(),
    },
  }
})

beforeEach(() => {
  nativeSurface.active = false
  nativeSurface.occluded = false
  vi.mocked(api.listSessions).mockResolvedValue([])
  vi.mocked(api.listMessages).mockResolvedValue([
    {
      id: 1,
      session_id: 7,
      role: 'user',
      content: 'Question',
      thinking: '',
      thinking_ms: 0,
      retrieval_trimmed: false,
      omitted_document_count: 0,
      tool_activity: [],
      created_at: '2026-08-04T12:00:00Z',
    } as MessageRead,
    {
      id: 2,
      session_id: 7,
      role: 'assistant',
      content: 'Answer',
      thinking: '',
      thinking_ms: 0,
      retrieval_trimmed: false,
      omitted_document_count: 0,
      tool_activity: [],
      created_at: '2026-08-04T12:00:01Z',
    } as MessageRead,
  ])
  vi.mocked(api.listDocuments).mockResolvedValue([])
  vi.mocked(api.getClassProfile).mockResolvedValue({ facts: [], extraction_skipped_reason: null })
  vi.mocked(api.getSettings).mockResolvedValue({
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
  } as SettingsRead)
})

it('shows the source picker over native chat, then returns focus to the composer', async () => {
  nativeSurface.active = true
  const onSelect = vi.fn()
  const document = {
    id: 5,
    class_id: 1,
    filename: 'Fourier notes.pdf',
    state: 'ready',
  } as DocumentRead
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <ChatPane
          classId={1}
          className="ECE 203"
          agent
          selectedDocumentId={null}
          sessionId={7}
          onSessionIdChange={() => {}}
          sourceControl={
            <SourceContext documents={[document]} selectedId={null} onSelect={onSelect} />
          }
        />
      </TooltipProvider>
    </QueryClientProvider>,
  )
  const user = userEvent.setup()
  await waitFor(() => expect(screen.queryByRole('region', { name: 'Conversation' })).toBeNull())
  await user.click(screen.getByRole('button', { name: /Choose what Lyra reads/ }))
  await waitFor(() => {
    expect(nativeSurface.occluded).toBe(true)
    expect(screen.getByRole('region', { name: 'Conversation' })).toBeInTheDocument()
  })
  const search = screen.getByRole('textbox', { name: "Search this class's files" })
  expect(search).toHaveFocus()
  await user.type(search, 'Fourier')
  await user.click(screen.getByRole('radio', { name: /Fourier notes.pdf/ }))
  expect(onSelect).toHaveBeenCalledWith(5)
  await waitFor(() => expect(nativeSurface.occluded).toBe(false))
  await user.type(screen.getByRole('textbox', { name: 'Message Lyra' }), 'Continue')
  expect(screen.getByRole('textbox', { name: 'Message Lyra' })).toHaveValue('Continue')
})

it('reattaches ordinary scrolling and content observation after native fallback', async () => {
  const observed: Element[] = []
  class Observer {
    observe(node: Element) {
      observed.push(node)
    }
    disconnect() {}
  }
  vi.stubGlobal('ResizeObserver', Observer)
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const pane = () => (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <ChatPane
          classId={1}
          className="ECE 203"
          agent
          selectedDocumentId={null}
          sessionId={7}
          onSessionIdChange={() => {}}
        />
      </TooltipProvider>
    </QueryClientProvider>
  )
  const view = render(pane())
  try {
    await screen.findByText('Answer')
    nativeSurface.active = true
    view.rerender(pane())
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Conversation' })).toBeNull())

    nativeSurface.active = false
    view.rerender(pane())
    const viewport = screen.getByRole('region', { name: 'Conversation' })
    Object.defineProperties(viewport, {
      scrollHeight: { configurable: true, value: 1000 },
      clientHeight: { configurable: true, value: 200 },
    })
    const frames: FrameRequestCallback[] = []
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) =>
      frames.push(callback),
    )
    viewport.scrollTop = 400
    act(() => {
      viewport.dispatchEvent(new WheelEvent('wheel'))
      viewport.dispatchEvent(new Event('scroll'))
      frames.shift()?.(0)
    })
    expect(screen.getByRole('button', { name: 'Jump to latest' })).toBeVisible()
    expect(observed.some((node) => viewport.contains(node))).toBe(true)
  } finally {
    view.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  }
})

it('keeps a streaming reader away from the tail after native overflow, then honors Jump to latest', async () => {
  nativeSurface.active = true
  let emit: ((event: AgentStreamEvent) => void) | undefined
  vi.mocked(api.sendAgentChat).mockImplementation((...args) => {
    emit = args[8]
    return new Promise(() => {})
  })
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const pane = () => (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <ChatPane
          classId={1}
          className="ECE 203"
          agent
          selectedDocumentId={null}
          sessionId={7}
          onSessionIdChange={() => {}}
        />
      </TooltipProvider>
    </QueryClientProvider>
  )
  const view = render(pane())
  const user = userEvent.setup()
  await user.type(await screen.findByRole('textbox', { name: 'Message Lyra' }), 'Next question')
  await user.click(screen.getByRole('button', { name: 'Send message' }))
  await waitFor(() => expect(emit).toBeTypeOf('function'))
  await act(async () => emit?.({ type: 'token', text: 'First words' }))
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 50))
  })

  nativeSurface.active = false
  view.rerender(pane())
  const viewport = screen.getByRole('region', { name: 'Conversation' })
  Object.defineProperties(viewport, {
    scrollHeight: { configurable: true, value: 1000 },
    clientHeight: { configurable: true, value: 200 },
  })
  const scrollTo = vi.fn(({ top }: ScrollToOptions) => {
    viewport.scrollTop = top ?? 0
  })
  viewport.scrollTo = scrollTo as typeof viewport.scrollTo
  viewport.scrollTop = 400
  const frames: FrameRequestCallback[] = []
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => frames.push(callback))
  try {
    act(() => {
      viewport.dispatchEvent(new WheelEvent('wheel'))
      viewport.dispatchEvent(new Event('scroll'))
      frames.shift()?.(0)
    })
    expect(screen.getByRole('button', { name: 'Jump to latest' })).toBeVisible()
    scrollTo.mockClear()

    await act(async () => emit?.({ type: 'token', text: ' and more words' }))
    act(() => {
      while (frames.length) frames.shift()?.(0)
    })
    expect(viewport.scrollTop).toBe(400)
    expect(scrollTo).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Jump to latest' }))
    expect(scrollTo).toHaveBeenCalled()
    expect(viewport.scrollTop).toBe(800)
  } finally {
    view.unmount()
    vi.restoreAllMocks()
  }
})
