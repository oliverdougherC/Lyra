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
 * mid-turn, and it is the whole point of the test below.
 */
function Workspace() {
  const [sessionId, setSessionId] = useState<number | null>(null)
  return (
    <ChatPane
      classId={1}
      className="ECE 203"
      selectedDocumentId={null}
      onClearSelectedDocument={() => {}}
      sessionId={sessionId}
      draft={sessionId === null}
      onSessionIdChange={setSessionId}
    />
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
})
