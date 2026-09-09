/**
 * PLA-510/511/509 work report (measurement, not an assertion suite): one ChatPane on a long
 * settled history, playing back composer keystrokes and a turn with reasoning/answer
 * bursts, with the per-phase cost read off the production work counters. Run against a
 * checkout and keep the printed report; the before/after pair is the evidence that a
 * settled transcript stops paying for every keystroke and live delta, and that the
 * long-answer reparse is bounded.
 *
 * What a number means:
 * - `rowRenders` is MessageRow renders in the phase (the live row counted separately), so
 *   a phase that should touch no settled row reports a delta of only its live rows.
 * - `handoffLookups` is row-key/selection handoff resolutions; `handoffScanSteps` is the
 *   containment work of the pre-index linear scan (zero once handoffs are indexed).
 * - `markdownNormalizations`/`normalizedChars` are the whole-document parses the live
 *   answer row actually ran, and how many characters of source each pass normalized.
 * - `answerPublications`/`reasoningPublications` are the text commits the rows received;
 *   `followScrolls` are stream-follow scroll operations that ran.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { ChatPane } from '@/components/chat/chat-pane'
import { TooltipProvider } from '@/components/ui/tooltip'
import { api, streamChat } from '@/lib/api'
import { chatWork } from '@/components/chat/work-counters'
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

const HISTORY_MESSAGES = 60

function historyMessages(count: number): MessageRead[] {
  const rows: MessageRead[] = []
  for (let i = 0; i < count; i += 1) {
    const n = Math.floor(i / 2) + 1
    rows.push({
      id: i * 2 + 1,
      session_id: 7,
      role: 'user',
      content: `Question ${n} about the material`,
      thinking: '',
      thinking_ms: 0,
      retrieval_trimmed: false,
      omitted_document_count: 0,
      tool_activity: [],
      created_at: `2026-08-04T1${String(n % 10)}:00:00Z`,
    })
    rows.push({
      id: i * 2 + 2,
      session_id: 7,
      role: 'assistant',
      content: `Answer ${n}. The system is stable when the pole sits left of the axis, so the decay is exponential and the tail is light.`,
      thinking: i % 3 === 0 ? `Thought ${n}: check the sign of the real part first.` : '',
      thinking_ms: i % 3 === 0 ? 1200 : 0,
      retrieval_trimmed: false,
      omitted_document_count: 0,
      tool_activity: [],
      created_at: `2026-08-04T1${String(n % 10)}:01:00Z`,
    })
  }
  return rows
}

type Phase = {
  name: string
  rowRenders: number
  liveRowRenders: number
  transcriptIterations: number
  handoffLookups: number
  handoffScanSteps: number
  markdownNormalizations: number
  normalizedChars: number
  revealNodeVisits: number
  answerPublications: number
  reasoningPublications: number
  followScrolls: number
}

function snapshotPhase(name: string, before: { [K in keyof typeof chatWork]: number }): Phase {
  return {
    name,
    rowRenders: chatWork.rowRenders - before.rowRenders,
    liveRowRenders: chatWork.liveRowRenders - before.liveRowRenders,
    transcriptIterations: chatWork.transcriptIterations - before.transcriptIterations,
    handoffLookups: chatWork.handoffLookups - before.handoffLookups,
    handoffScanSteps: chatWork.handoffScanSteps - before.handoffScanSteps,
    markdownNormalizations: chatWork.markdownNormalizations - before.markdownNormalizations,
    normalizedChars: chatWork.normalizedChars - before.normalizedChars,
    revealNodeVisits: chatWork.revealNodeVisits - before.revealNodeVisits,
    answerPublications: chatWork.answerPublications - before.answerPublications,
    reasoningPublications: chatWork.reasoningPublications - before.reasoningPublications,
    followScrolls: chatWork.followScrolls - before.followScrolls,
  }
}

function zeros(): { [K in keyof typeof chatWork]: number } {
  return { ...chatWork }
}

describe('ChatPane work report (PLA-510/511/509)', () => {
  it('long settled history + bursts + typing', async () => {
    const transcript: MessageRead[] = historyMessages(HISTORY_MESSAGES)
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
    vi.mocked(api.listDocuments).mockResolvedValue([])
    vi.mocked(api.getClassProfile).mockResolvedValue({ facts: [], extraction_skipped_reason: null })
    vi.mocked(api.getSettings).mockResolvedValue({
      endpoint_url: 'http://localhost:1234/v1',
      model: 'local',
    } as Awaited<ReturnType<typeof api.getSettings>>)
    vi.mocked(api.listMessages).mockImplementation(async () => [...transcript])
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

    // The history has landed: the pane is reading a settled conversation. (The text
    // matcher also hits the row's inner paragraph, so at least one match is the check.)
    const composer = await screen.findByLabelText('Message Lyra')
    expect((await screen.findAllByText(/Answer 30/)).length).toBeGreaterThanOrEqual(1)

    // Phase 1: typing in the composer over the settled history.
    const p1 = zeros()
    await user.type(composer, 'Another question about modes')
    const phase1 = snapshotPhase('typing-25-keystrokes', p1)

    // Turn 1: a short answer that settles, so a handoff exists for the next phase.
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalled())
    await act(async () => {
      stream.emit?.({ type: 'token', text: 'Short settled answer.' })
      stream.emit?.({ type: 'done', message_id: 901 })
      transcript.push(
        {
          id: 900,
          session_id: 7,
          role: 'user',
          content: 'Another question about modes',
          thinking: '',
          thinking_ms: 0,
          retrieval_trimmed: false,
          omitted_document_count: 0,
          tool_activity: [],
          created_at: '2026-08-05T09:00:00Z',
        },
        {
          id: 901,
          session_id: 7,
          role: 'assistant',
          content: 'Short settled answer.',
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

    // Phase 2: a turn with a reasoning burst and an answer burst, with the handoff present.
    await user.type(composer, 'Now with reasoning')
    await user.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(2))
    const p2 = zeros()
    await act(async () => {
      for (let i = 0; i < 40; i += 1) {
        stream.emit?.({ type: 'reasoning', text: `thought piece ${i} ` })
      }
    })
    await act(async () => {
      for (let i = 0; i < 40; i += 1) {
        stream.emit?.({ type: 'token', text: `answer piece ${i} ` })
      }
    })
    // Let any frame-coalesced publications fire.
    await act(async () => new Promise((resolve) => setTimeout(resolve, 200)))
    const phase2 = snapshotPhase('reasoning+answer-bursts', p2)
    // The live answer is split across per-word reveal spans, so match on the accumulated
    // text content of any element rather than a single node.
    const burstLanded = screen.getAllByText((_content, element) => {
      // The live answer is split across per-word reveal spans, so match on the
      // accumulated text content of an element (the row container holds it all).
      return (element?.textContent ?? '').includes('answer piece 39')
    }).length
    expect(burstLanded, 'the burst must land on screen').toBeGreaterThanOrEqual(1)

    // Phase 3: settle the turn.
    await act(async () => {
      stream.emit?.({ type: 'done', message_id: 903 })
      transcript.push(
        {
          id: 902,
          session_id: 7,
          role: 'user',
          content: 'Now with reasoning',
          thinking: '',
          thinking_ms: 0,
          retrieval_trimmed: false,
          omitted_document_count: 0,
          tool_activity: [],
          created_at: '2026-08-05T09:05:00Z',
        },
        {
          id: 903,
          session_id: 7,
          role: 'assistant',
          content: Array.from({ length: 40 }, (_, i) => `answer piece ${i} `).join(''),
          thinking: Array.from({ length: 40 }, (_, i) => `thought piece ${i} `).join(''),
          thinking_ms: 900,
          retrieval_trimmed: false,
          omitted_document_count: 0,
          tool_activity: [],
          created_at: '2026-08-05T09:05:01Z',
        },
      )
      stream.finish?.()
    })
    await waitFor(() => expect(api.listMessages).toHaveBeenCalledTimes(3), { timeout: 5000 })

    // Phase 4: typing again after the settle (handoffs for two turns now present).
    const p4 = zeros()
    await user.type(composer, 'One more')
    const phase4 = snapshotPhase('typing-after-settle', p4)

    const report = {
      history: HISTORY_MESSAGES,
      phase1,
      phase2,
      phase4,
    }
    console.log(`WORK-REPORT ${JSON.stringify(report)}`)
    // Sanity: the report measured real work. These hold both before the optimization
    // (the baseline the before/after pair is measured against) and after it — the turn
    // must always publish its reasoning and answer and re-parse the live document. The
    // rowRenders deltas are NOT sanity-checked: the whole point of the work is that a
    // keystroke or a live delta may stop touching settled rows entirely, so a zero there
    // is a win, not a failure.
    expect(phase2.reasoningPublications).toBeGreaterThan(0)
    expect(phase2.answerPublications).toBeGreaterThan(0)
    expect(phase2.markdownNormalizations).toBeGreaterThan(0)

    // The settled-transcript boundary (PLA-510): composer typing over a settled history
    // must not re-run the O(history) handoff/key work at all — zero settled-row iterations
    // and zero handoff lookups in a pure-typing phase.
    expect(phase1.transcriptIterations).toBe(0)
    expect(phase1.handoffLookups).toBe(0)
    expect(phase4.transcriptIterations).toBe(0)
    expect(phase4.handoffLookups).toBe(0)
    // During an active turn the settled set re-derives at most once (the send changes the
    // pending slice), never per burst: iterations/lookups stay bounded by one pass over the
    // settled rows, not by the number of reasoning/answer deltas.
    expect(phase2.transcriptIterations).toBeLessThanOrEqual(HISTORY_MESSAGES + 2)
    expect(phase2.handoffLookups).toBeLessThanOrEqual(HISTORY_MESSAGES + 2)
  }, 120_000)

  it('short settled history: typing still costs zero settled-row work', async () => {
    // A tiny history proves the zero iterations/lookups in a typing phase is the memo
    // bailing out — not an artifact of a large history where the work would be hidden.
    const transcript: MessageRead[] = historyMessages(4)
    vi.mocked(api.listSessions).mockResolvedValue([
      {
        id: 7,
        class_id: 1,
        title: 'Short discussion',
        mode: 'guide',
        artifact_part_id: null,
        created_at: '2026-08-04T12:00:00Z',
      },
    ])
    vi.mocked(api.listDocuments).mockResolvedValue([])
    vi.mocked(api.getClassProfile).mockResolvedValue({ facts: [], extraction_skipped_reason: null })
    vi.mocked(api.getSettings).mockResolvedValue({
      endpoint_url: 'http://localhost:1234/v1',
      model: 'local',
    } as Awaited<ReturnType<typeof api.getSettings>>)
    vi.mocked(api.listMessages).mockImplementation(async () => [...transcript])

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
    expect((await screen.findAllByText(/Answer 2/)).length).toBeGreaterThanOrEqual(1)

    const p1 = zeros()
    await user.type(composer, 'A short follow-up question')
    const phase1 = snapshotPhase('typing-short-history', p1)
    console.log(`WORK-REPORT-SHORT ${JSON.stringify({ history: 4, phase1 })}`)
    expect(phase1.transcriptIterations).toBe(0)
    expect(phase1.handoffLookups).toBe(0)
  }, 60_000)
})
