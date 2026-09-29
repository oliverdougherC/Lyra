/**
 * PLA-510/509 behavior contracts (assertions, not measurements — the measured before/after
 * lives in chat-work-report.test.tsx): while a live turn moves, the pane's work stays
 * bounded — a burst of reasoning or answer events commits at most a handful of
 * publications, never one per event; the settled transcript renders zero rows for live
 * deltas and composer keystrokes; and a genuinely hidden document does no presentation
 * work at all until the reader returns, with terminal frames still flushing synchronously.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ChatPane } from '@/components/chat/chat-pane'
import { TooltipProvider } from '@/components/ui/tooltip'
import { api, streamChat } from '@/lib/api'
import { chatWork, resetChatWork } from '@/components/chat/work-counters'
import type { ChatEvent, MessageRead } from '@/types'

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
    },
    streamChat: vi.fn(),
  }
})

function historyMessages(count: number): MessageRead[] {
  const rows: MessageRead[] = []
  for (let i = 0; i < count; i += 1) {
    rows.push(
      {
        id: i * 2 + 1,
        session_id: 7,
        role: 'user',
        content: `Question ${i + 1}`,
        thinking: '',
        thinking_ms: 0,
        retrieval_trimmed: false,
        omitted_document_count: 0,
        tool_activity: [],
        created_at: `2026-08-04T1${String(i % 10)}:00:00Z`,
      },
      {
        id: i * 2 + 2,
        session_id: 7,
        role: 'assistant',
        content: `Answer ${i + 1}. The decay is exponential, so the tail is light.`,
        thinking: '',
        thinking_ms: 0,
        retrieval_trimmed: false,
        omitted_document_count: 0,
        tool_activity: [],
        created_at: `2026-08-04T1${String(i % 10)}:01:00Z`,
      },
    )
  }
  return rows
}

function setVisibility(state: string) {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: state })
}

/** Render the tutor pane on a settled history and start one turn. */
async function startTurn(transcript: MessageRead[]): Promise<{
  emit: (event: ChatEvent) => void
  finish: () => void
  container: HTMLElement
  user: ReturnType<typeof userEvent.setup>
}> {
  const stream: { emit?: (event: ChatEvent) => void; finish?: () => void } = {}
  vi.mocked(api.listSessions).mockResolvedValue([
    {
      id: 7,
      class_id: 1,
      title: 'Saved discussion',
      mode: 'guide',
      artifact_part_id: null,
      created_at: '2026-08-04T12:00:00Z',
    },
  ])
  vi.mocked(api.listMessages).mockImplementation(async () => [...transcript])
  vi.mocked(api.listDocuments).mockResolvedValue([])
  vi.mocked(api.getClassProfile).mockResolvedValue({ facts: [], extraction_skipped_reason: null })
  vi.mocked(api.getSettings).mockResolvedValue({
    endpoint_url: 'http://localhost:1234/v1',
    model: 'local',
  } as Awaited<ReturnType<typeof api.getSettings>>)
  vi.mocked(streamChat).mockImplementation((_sessionId, _body, onEvent) => {
    stream.emit = onEvent
    return new Promise<void>((resolve) => {
      stream.finish = resolve
    })
  })

  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const user = userEvent.setup()
  const { container } = render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <ChatPane classId={1} className="ECE 203" selectedDocumentId={null} sessionId={7} />
      </TooltipProvider>
    </QueryClientProvider>,
  )
  const composer = await screen.findByLabelText('Message Lyra')
  await user.type(composer, 'A burst question')
  await user.click(screen.getByLabelText('Send message'))
  await waitFor(() => expect(streamChat).toHaveBeenCalled())
  return {
    emit: (event) => stream.emit?.(event),
    finish: () => stream.finish?.(),
    container,
    user,
  }
}

/** Rows that rendered that were NOT the live row. */
function settledRowRenders(before: { [K in keyof typeof chatWork]: number }): number {
  return chatWork.rowRenders - before.rowRenders - (chatWork.liveRowRenders - before.liveRowRenders)
}

describe('ChatPane live isolation (PLA-510/509)', () => {
  afterEach(() => {
    setVisibility('visible')
    vi.unstubAllGlobals()
    resetChatWork()
    // The api mocks are module-level and their call counts otherwise leak across tests,
    // so a later `toHaveBeenCalledTimes` would see an earlier turn's refetches too.
    vi.mocked(api.listSessions).mockClear()
    vi.mocked(api.listMessages).mockClear()
    vi.mocked(api.listDocuments).mockClear()
    vi.mocked(api.getClassProfile).mockClear()
    vi.mocked(api.getSettings).mockClear()
    vi.mocked(api.createSession).mockClear()
    vi.mocked(streamChat).mockClear()
  })

  it('coalesces reasoning and answer bursts into few publications, bytes intact', async () => {
    const transcript = historyMessages(4)
    const { emit, container, user } = await startTurn(transcript)
    try {
      // The history has settled: measure from here, so the burst's work is what counts.
      resetChatWork()
      const before = { ...chatWork }
      await act(async () => {
        for (let i = 0; i < 40; i += 1) {
          emit({ type: 'reasoning', text: `thought piece ${i} ` })
        }
      })
      await act(async () => {
        for (let i = 0; i < 40; i += 1) {
          emit({ type: 'token', text: `answer piece ${i} ` })
        }
      })
      // Let the coalesced frames fire and drain.
      await act(async () => new Promise((resolve) => setTimeout(resolve, 200)))

      // Eighty events, a handful of commits: the first word of each channel publishes
      // immediately and the rest coalesce one per frame. (The baseline published 40
      // reasoning commits for this burst — one per event.)
      expect(chatWork.reasoningPublications - before.reasoningPublications).toBeLessThanOrEqual(3)
      expect(chatWork.answerPublications - before.answerPublications).toBeLessThanOrEqual(3)

      // The settled transcript paid nothing for the burst: every row render was the live
      // row's own.
      expect(settledRowRenders(before)).toBe(0)

      // Byte fidelity, answer: all 40 pieces are on screen. (The count is the exact
      // check — "piece 1" would also match inside "piece 10". The paragraph's final
      // trailing space is normalized away by Markdown rendering, so no trailing space is
      // expected.)
      const page = container.textContent ?? ''
      for (let i = 0; i < 40; i += 1) {
        expect(page, `answer piece ${i}`).toContain(`answer piece ${i}`)
      }
      expect(page.match(/answer piece \d+/g)?.length).toBe(40)

      // Byte fidelity, reasoning: the live disclosure is closed by design (a thought is
      // the model's working, not the answer), so the text is not in the DOM until the
      // reader opens it. The bytes were never dropped — opening it shows the whole
      // thought, in order. The live trace's header reads exactly 'Thought' while the
      // answer is moving, so it is addressable by its label.
      const thoughtLabel = screen.getAllByText('Thought').at(-1)
      const trigger = thoughtLabel?.closest('button')
      expect(trigger, 'the live reasoning disclosure').toBeDefined()
      await user.click(trigger as HTMLButtonElement)
      const opened = container.textContent ?? ''
      for (let i = 0; i < 40; i += 1) {
        expect(opened, `thought piece ${i}`).toContain(`thought piece ${i}`)
      }
      expect(opened.match(/thought piece \d+/g)?.length).toBe(40)
    } finally {
      resetChatWork()
    }
  }, 120_000)

  it('holds a closed reasoning disclosure under paced events, then flushes it whole on open and terminal', async () => {
    // One reasoning event per animation frame (not a synchronous burst), with the live
    // disclosure CLOSED by design. The closed disclosure owes no per-delta publications —
    // its header keeps itself alive — so the only publication is the first word; every
    // later event just refreshes the buffer's pending slot (PLA-509).
    const transcript = historyMessages(2)
    const { emit, finish, container, user } = await startTurn(transcript)
    resetChatWork()
    const before = { ...chatWork }
    try {
      const N = 20
      for (let i = 0; i < N; i += 1) {
        await act(async () => emit({ type: 'reasoning', text: `step ${i} ` }))
        // Pace to the frame cadence: one event per animation frame.
        await act(async () => new Promise((resolve) => setTimeout(resolve, 20)))
      }

      // Closed, paced: exactly one publication (the first word). The 19 later events did
      // not publish the closed disclosure on each delta.
      expect(chatWork.reasoningPublications - before.reasoningPublications).toBe(1)
      // And the held thought is not in the DOM while closed.
      expect(container.textContent ?? '').not.toContain('step 19')

      // Opening is the moment the held thought becomes visible: the transport buffer
      // flushes in full, on this microtask, so the reader sees the complete reasoning to
      // date — not just the last published frame. (While the thought is still arriving the
      // header is a live indicator, not the settled 'Thought' word, so address the
      // disclosure by its stable slot, not its label.)
      const trigger = container.querySelector<HTMLButtonElement>(
        '[data-slot="collapsible-trigger"]',
      )
      expect(trigger, 'the live reasoning disclosure').toBeDefined()
      await user.click(trigger as HTMLButtonElement)
      const opened = container.textContent ?? ''
      for (let i = 0; i < N; i += 1) {
        expect(opened, `step ${i} after opening`).toContain(`step ${i}`)
      }

      // Open and paced: the disclosure now publishes at frame cadence (coalesced), still
      // far fewer commits than events.
      const M = 10
      for (let i = 0; i < M; i += 1) {
        await act(async () => emit({ type: 'reasoning', text: `more ${i} ` }))
        await act(async () => new Promise((resolve) => setTimeout(resolve, 20)))
      }

      // Terminal: the turn settles, the streaming row is replaced by the stored message,
      // and the full thought must travel with it — not strand in the live buffer. The
      // terminal flush publishes the buffer before the swap; the stored message then keeps
      // the complete reasoning under its own collapsed Details disclosure.
      await act(async () => {
        emit({ type: 'done', message_id: 301 })
        transcript.push(
          {
            id: 300,
            session_id: 7,
            role: 'user',
            content: 'A burst question',
            thinking: '',
            thinking_ms: 0,
            retrieval_trimmed: false,
            omitted_document_count: 0,
            tool_activity: [],
            created_at: '2026-08-05T09:00:00Z',
          },
          {
            id: 301,
            session_id: 7,
            role: 'assistant',
            content: 'Done.',
            thinking:
              Array.from({ length: N }, (_, i) => `step ${i} `).join('') +
              Array.from({ length: M }, (_, i) => `more ${i} `).join(''),
            thinking_ms: 1500,
            retrieval_trimmed: false,
            omitted_document_count: 0,
            tool_activity: [],
            created_at: '2026-08-05T09:00:01Z',
          },
        )
        finish()
      })
      await waitFor(() => expect(api.listMessages).toHaveBeenCalledTimes(2), { timeout: 5000 })
      // Open the settled record: the whole paced thought is there, in order.
      const details = container.querySelector('[data-slot="collapsible-trigger"]')
      expect(details, 'the settled turn Details disclosure').toBeDefined()
      await user.click(details as HTMLElement)
      const final = container.textContent ?? ''
      for (let i = 0; i < N + M; i += 1) {
        expect(final, `reasoning ${i} after terminal`).toContain(
          i < N ? `step ${i}` : `more ${i - N}`,
        )
      }
      // The whole paced stream committed a handful of frames, never one per event.
      expect(chatWork.reasoningPublications - before.reasoningPublications).toBeLessThan(N + M)
    } finally {
      resetChatWork()
    }
  }, 120_000)

  it('renders no settled row for composer keystrokes after the transcript settles', async () => {
    const transcript = historyMessages(5)
    const stream: { emit?: (event: ChatEvent) => void; finish?: () => void } = {}
    vi.mocked(api.listSessions).mockResolvedValue([
      {
        id: 7,
        class_id: 1,
        title: 'Saved discussion',
        mode: 'guide',
        artifact_part_id: null,
        created_at: '2026-08-04T12:00:00Z',
      },
    ])
    vi.mocked(api.listMessages).mockImplementation(async () => [...transcript])
    vi.mocked(api.listDocuments).mockResolvedValue([])
    vi.mocked(api.getClassProfile).mockResolvedValue({ facts: [], extraction_skipped_reason: null })
    vi.mocked(api.getSettings).mockResolvedValue({
      endpoint_url: 'http://localhost:1234/v1',
      model: 'local',
    } as Awaited<ReturnType<typeof api.getSettings>>)
    vi.mocked(streamChat).mockImplementation((_sessionId, _body, onEvent) => {
      stream.emit = onEvent
      return new Promise<void>((resolve) => {
        stream.finish = resolve
      })
    })
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    const user = userEvent.setup()
    render(
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <ChatPane classId={1} className="ECE 203" selectedDocumentId={null} sessionId={7} />
        </TooltipProvider>
      </QueryClientProvider>,
    )
    const composer = await screen.findByLabelText('Message Lyra')

    // One short turn, settled: the pane now reads a ten-row transcript plus the handoff.
    await user.type(composer, 'Quick question')
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalled())
    await act(async () => {
      stream.emit?.({ type: 'token', text: 'A short settled answer.' })
      stream.emit?.({ type: 'done', message_id: 101 })
      transcript.push(
        {
          id: 100,
          session_id: 7,
          role: 'user',
          content: 'Quick question',
          thinking: '',
          thinking_ms: 0,
          retrieval_trimmed: false,
          omitted_document_count: 0,
          tool_activity: [],
          created_at: '2026-08-05T09:00:00Z',
        },
        {
          id: 101,
          session_id: 7,
          role: 'assistant',
          content: 'A short settled answer.',
          thinking: '',
          thinking_ms: 0,
          retrieval_trimmed: false,
          omitted_document_count: 0,
          tool_activity: [],
          created_at: '2026-08-05T09:00:01Z',
        },
      )
      stream.finish?.()
    })
    await waitFor(() => expect(api.listMessages).toHaveBeenCalledTimes(2), { timeout: 5000 })

    resetChatWork()
    const before = { ...chatWork }
    await user.type(composer, 'Keep going')
    // (The baseline re-rendered every settled row on every keystroke.)
    expect(settledRowRenders(before)).toBe(0)
    // Handoffs are indexed: lookups do a constant-time find, never a scan.
    expect(chatWork.handoffScanSteps - before.handoffScanSteps).toBe(0)
  }, 120_000)

  it('holds every hidden publication, does no per-token frame work, and flushes the terminal frame', async () => {
    let rafCount = 0
    vi.stubGlobal('requestAnimationFrame', () => {
      rafCount += 1
      return 0
    })
    const transcript = historyMessages(2)
    setVisibility('hidden')
    const { emit, finish, container } = await startTurn(transcript)
    // The turn-start follow effect arms one frame per turn (a placement, not a
    // publication): the hidden contract is about what the TOKENS do, so baseline from
    // here.
    const atTurnStart = rafCount
    try {
      // The first word publishes immediately — even hidden, the pane shows the turn began.
      await act(async () => {
        emit({ type: 'token', text: 'One ' })
      })
      expect(container.textContent).toContain('One')

      // A genuinely hidden window is owed no frames per token: the rest is held — no rAF,
      // no timer, no commit. The old 64 ms backstop timer used to publish here.
      await act(async () => {
        emit({ type: 'token', text: 'two ' })
        emit({ type: 'token', text: 'three ' })
        await new Promise((resolve) => setTimeout(resolve, 200))
      })
      expect(rafCount).toBe(atTurnStart)
      expect(container.textContent).toContain('One')
      expect(container.textContent).not.toContain('two three')

      // The terminal frame flushes synchronously, hidden or visible: the last words never
      // wait for a frame that a hidden document is owed.
      await act(async () => {
        emit({ type: 'done', message_id: 201 })
        transcript.push(
          {
            id: 200,
            session_id: 7,
            role: 'user',
            content: 'A burst question',
            thinking: '',
            thinking_ms: 0,
            retrieval_trimmed: false,
            omitted_document_count: 0,
            tool_activity: [],
            created_at: '2026-08-05T09:00:00Z',
          },
          {
            id: 201,
            session_id: 7,
            role: 'assistant',
            content: 'One two three ',
            thinking: '',
            thinking_ms: 0,
            retrieval_trimmed: false,
            omitted_document_count: 0,
            tool_activity: [],
            created_at: '2026-08-05T09:00:01Z',
          },
        )
        finish()
      })
      expect(container.textContent).toContain('One two three')
      expect(rafCount).toBe(atTurnStart)
    } finally {
      setVisibility('visible')
    }
  }, 120_000)

  it('reconciles on every hide/show cycle without arming a new frame each time', async () => {
    let rafCount = 0
    vi.stubGlobal('requestAnimationFrame', () => {
      rafCount += 1
      return 0
    })
    const transcript = historyMessages(1)
    const { emit, container } = await startTurn(transcript)
    try {
      // Visible: the first word is immediate, the rest coalesce into one pending frame.
      // A bounded handful of frames: the turn-start placement, the coalesced publication
      // frame, and the follow's deduplicated re-pin frame (PLA-510) — never one per token.
      await act(async () => {
        emit({ type: 'token', text: 'a ' })
        for (let i = 0; i < 15; i += 1) emit({ type: 'token', text: 'b ' })
      })
      const armedOnce = rafCount
      expect(armedOnce).toBeGreaterThan(0)
      expect(armedOnce).toBeLessThanOrEqual(3)

      // Hide: publications hold and arm nothing — the window is owed no frames.
      setVisibility('hidden')
      await act(async () => {
        for (let i = 0; i < 10; i += 1) emit({ type: 'token', text: 'c ' })
        await new Promise((resolve) => setTimeout(resolve, 150))
      })
      expect(rafCount).toBe(armedOnce)

      // Return: whatever gathered while away is published — and no extra frame was needed.
      setVisibility('visible')
      await act(async () => {
        document.dispatchEvent(new Event('visibilitychange'))
      })
      expect(container.textContent).toContain(('a ' + 'b '.repeat(15) + 'c '.repeat(10)).trimEnd())
      expect(rafCount).toBe(armedOnce)

      // Hide again: the invariant holds for the next cycle, not just the first.
      setVisibility('hidden')
      await act(async () => {
        for (let i = 0; i < 10; i += 1) emit({ type: 'token', text: 'd ' })
        await new Promise((resolve) => setTimeout(resolve, 150))
      })
      expect(rafCount).toBe(armedOnce)
      setVisibility('visible')
      await act(async () => {
        document.dispatchEvent(new Event('visibilitychange'))
      })
      expect(container.textContent).toContain(('c '.repeat(10) + 'd '.repeat(10)).trimEnd())
    } finally {
      setVisibility('visible')
    }
  }, 120_000)
})
