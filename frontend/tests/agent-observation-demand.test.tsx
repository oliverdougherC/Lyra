/**
 * PLA-509: the agent work queries and the background-job queries (documents, drafts,
 * study) observe on demand, and a hidden window suspends - never loses - the observation:
 * nothing is sent while hidden (so nothing cancels server-side work), one authoritative
 * read reconciles on visibility return, and a visible-but-unfocused window keeps
 * observing.
 *
 * The regression this file guards: a settled conversation used to keep a 42-to-102
 * requests-per-minute heartbeat (activity every 2s, dismissals every 5s, changes and
 * commands every 2s with a workspace) no matter what. Now the queries ask again only
 * when there is a reason - a live turn (announced by the pane through the owner-scoped
 * turn signal, or a mutation this surface started), a command that is actually running,
 * a dismissal approaching its lapse, or a reconciliation on window return. Pending
 * approvals and open proposals are settled decisions and never open the cadence. A
 * settled conversation makes zero unexplained periodic requests, and a failing read
 * retries at the bounded slow tier instead of a rapid loop. The before/after counts for
 * the same 60s foreground idle scenario (44/106 total -> 2/4 total) are recorded in the
 * PLA-509 handoff.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, renderHook, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AgentWorkSurface } from '@/components/agent/work-surface'
import { WorkspaceAttachProvider } from '@/components/agent/workspace-attach'
import { api } from '@/lib/api'
import {
  agentKeys,
  AGENT_ERROR_POLL_MS,
  AGENT_POLL_MS,
  agentPollInterval,
  beginAgentTurnObservation,
  dismissalPollInterval,
  DISMISSAL_POLL_MARGIN_MS,
  endAgentTurnObservation,
  useAgentAccessDismissals,
  useAgentActivity,
  useAgentTurnsLive,
} from '@/lib/hooks/use-agent'
import { useDocumentStatus, useDocuments } from '@/lib/hooks/use-documents'
import { useDrafts } from '@/lib/hooks/use-drafts'
import { useStudyList } from '@/lib/hooks/use-study'
import type {
  AgentAuditEventRead,
  AgentCommandRequestRead,
  AgentWorkspaceChangeRead,
  AgentWorkspaceRead,
  DocumentStatus,
  MessageRead,
  StudyListRead,
} from '@/types'

vi.mock('@/components/drafts/source-ledger', () => ({
  SourceLedger: () => null,
}))

const CLASS_ID = 9
const SESSION_ID = 11

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <WorkspaceAttachProvider classId={CLASS_ID}>{children}</WorkspaceAttachProvider>
    </QueryClientProvider>
  )
  return { queryClient, wrapper }
}

function workspace(
  overrides: Partial<AgentWorkspaceRead> = {},
): AgentWorkspaceRead {
  return {
    id: 1,
    class_id: CLASS_ID,
    root_path: '/tmp/proj',
    display_name: 'proj',
    read_enabled: true,
    change_proposals_enabled: true,
    commands_enabled: true,
    created_at: '2026-09-02T00:00:00Z',
    updated_at: '2026-09-02T00:00:00Z',
    ...overrides,
  }
}

function accessEvent(overrides: Partial<AgentAuditEventRead> = {}): AgentAuditEventRead {
  return {
    id: 'ev-1',
    tool: 'request_workspace_access',
    capability: 'access_request',
    effect: 'pure',
    state: 'succeeded',
    target_kind: 'capability_request',
    target_id: 'read',
    error_message: null,
    started_at: '2026-09-02T00:00:00Z',
    finished_at: '2026-09-02T00:00:01Z',
    result_summary: { scope: 'read' },
    ...overrides,
  }
}

function runningCommand(
  overrides: Partial<AgentCommandRequestRead> = {},
): AgentCommandRequestRead {
  return {
    id: 7,
    workspace_id: 1,
    session_id: SESSION_ID,
    argv: ['pytest'],
    relative_cwd: '.',
    reason: 'Check the tests',
    expected_signal: null,
    timeout_seconds: 60,
    state: 'running',
    confirmed_at: '2026-09-02T00:00:00Z',
    exit_code: null,
    stdout_text: null,
    stderr_text: null,
    truncated: false,
    ...overrides,
  }
}

function failedTurnMessage(): MessageRead {
  return {
    id: 3,
    session_id: SESSION_ID,
    role: 'user',
    content: 'Fix the parser',
    thinking: '',
    thinking_ms: 0,
    retrieval_trimmed: false,
    omitted_document_count: 0,
    tool_activity: [],
    created_at: '2026-09-02T00:00:00Z',
    agent_attempt: {
      state: 'failed',
      stopped_reason: 'timeout',
      detail: 'The tool loop timed out.',
    },
  }
}

function documentStatus(state: DocumentStatus['state']): DocumentStatus {
  return {
    id: 1,
    state,
    stage_detail: null,
    pages_total: 10,
    pages_done: 10,
    pages_skipped: 0,
    pages_failed: 0,
    recognize: false,
    error_message: null,
  } as DocumentStatus
}

/** jsdom has no real visibility model; the platform state react-query reads is stubbed. */
function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => state,
  })
  window.dispatchEvent(new Event('visibilitychange'))
}

beforeEach(() => {
  vi.useFakeTimers()
  // A failed test may have left the window hidden; observation tests assume a visible
  // start (jsdom's visibility stub is process-global, not per-test).
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => 'visible',
  })
  vi.spyOn(api, 'getAgentWorkspace').mockResolvedValue(null)
  vi.spyOn(api, 'listAgentActivity').mockResolvedValue([])
  vi.spyOn(api, 'listAgentAccessDismissals').mockResolvedValue({ dismissals: [] })
  vi.spyOn(api, 'listAgentWorkspaceChanges').mockResolvedValue([])
  vi.spyOn(api, 'listAgentCommands').mockResolvedValue([])
  vi.spyOn(api, 'listMessages').mockResolvedValue([])
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

// ── The interval decisions (pure) ───────────────────────────────────────────────────────

describe('agentPollInterval', () => {
  const state = (error: unknown = null) => ({ data: [], error })

  it('goes quiet when nothing is live', () => {
    expect(agentPollInterval(state(), false)).toBe(false)
  })

  it('keeps the live cadence while a job or command is in flight', () => {
    expect(agentPollInterval(state(), true)).toBe(AGENT_POLL_MS)
  })

  it('retries at the bounded slow cadence while failing, instead of failing rapidly forever', () => {
    expect(agentPollInterval(state(new Error('offline')), false)).toBe(AGENT_ERROR_POLL_MS)
    // The failure branch wins even while work is in flight: a dead backend is not
    // hammered at the live cadence.
    expect(agentPollInterval(state(new Error('offline')), true)).toBe(AGENT_ERROR_POLL_MS)
  })
})

describe('dismissalPollInterval', () => {
  const now = Date.parse('2026-09-02T00:00:00Z')

  it('is quiet with no active dismissal - nothing can change the list until the student acts', () => {
    expect(dismissalPollInterval({ data: { dismissals: [] }, error: null }, now)).toBe(false)
    expect(dismissalPollInterval({ data: undefined, error: null }, now)).toBe(false)
  })

  it('re-checks just after the earliest dismissal lapses, on the server TTL clock', () => {
    // Dismissed 1 minute before `now`: it lapses 29 minutes from now, and the one
    // justified re-check lands margin later than that.
    const interval = dismissalPollInterval(
      {
        data: { dismissals: [{ scope: 'read', dismissed_at: '2026-09-01T23:59:00Z' }] },
        error: null,
      },
      now,
    )
    expect(interval).toBe(29 * 60_000 + DISMISSAL_POLL_MARGIN_MS)
  })

  it('schedules from the earliest lapse when several dismissals are active', () => {
    // 10 minutes ago lapses in 20; 20 minutes ago lapses in 10 - the earliest wins.
    const interval = dismissalPollInterval(
      {
        data: {
          dismissals: [
            { scope: 'read', dismissed_at: '2026-09-01T23:50:00Z' },
            { scope: 'attach', dismissed_at: '2026-09-01T23:40:00Z' },
          ],
        },
        error: null,
      },
      now,
    )
    expect(interval).toBe(10 * 60_000 + DISMISSAL_POLL_MARGIN_MS)
  })

  it('confirms an already-lapsed dismissal with a bounded short check, never a rapid loop', () => {
    const interval = dismissalPollInterval(
      { data: { dismissals: [{ scope: 'read', dismissed_at: '2026-08-31T12:00:00Z' }] }, error: null },
      now,
    )
    expect(interval).toBe(DISMISSAL_POLL_MARGIN_MS)
  })

  it('falls back to the bounded error cadence while failing', () => {
    expect(dismissalPollInterval({ data: undefined, error: new Error('offline') }, now)).toBe(
      AGENT_ERROR_POLL_MS,
    )
  })
})

// ── The observation hooks with fake time and captured requests ──────────────────────────

describe('settled chat makes no periodic requests', () => {
  it('without a workspace: the initial reads are the only requests in a 60s idle', async () => {
    const { wrapper } = createWrapper()
    renderHook(() => useAgentActivity(CLASS_ID, SESSION_ID, false), { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    // Before this change the same 60s idle made 31 activity requests (30 of them a
    // timer, not a reason); a settled conversation now asks nothing after the initial read.
    expect(vi.mocked(api.listAgentActivity)).toHaveBeenCalledTimes(1)
  })

  it('with a live job: the 2-second cadence is back, and it follows the demand', async () => {
    vi.spyOn(api, 'listAgentActivity').mockResolvedValue([
      accessEvent({ tool: 'inspect_file', state: 'started', finished_at: null }),
    ])
    const { wrapper } = createWrapper()
    renderHook(() => useAgentActivity(CLASS_ID, SESSION_ID, true), { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    // Initial read plus the live observation at 2s: 1 + 5.
    expect(vi.mocked(api.listAgentActivity)).toHaveBeenCalledTimes(6)
  })
})

describe('dismissal expiry (PLA-509 TTL reconciliation)', () => {
  it('stays dismissed, re-checks once at the lapse, then goes quiet', async () => {
    vi.setSystemTime(new Date('2026-09-02T00:00:00Z'))
    const list = vi.spyOn(api, 'listAgentAccessDismissals')
    list
      .mockResolvedValueOnce({
        dismissals: [{ scope: 'read', dismissed_at: '2026-09-01T23:59:00Z' }],
      })
      .mockResolvedValue({ dismissals: [] })
    const { wrapper } = createWrapper()
    renderHook(() => useAgentAccessDismissals(CLASS_ID, SESSION_ID), { wrapper })

    // 28 minutes in: the dismissal (lapses in 29, re-check at 29 + the margin) is still
    // active and the query has not re-asked - the old code would have asked 5 more
    // times in this window.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(28 * 60_000)
    })
    expect(list).toHaveBeenCalledTimes(1)

    // Cross the re-check moment (29 minutes + 5s margin): exactly one reconciliation
    // read, and it finds the list empty - the card may resurface - so the query goes
    // quiet again.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(70_000)
    })
    expect(list).toHaveBeenCalledTimes(2)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60_000)
    })
    expect(list).toHaveBeenCalledTimes(2)
  })
})

describe('backend failures back off instead of failing rapidly', () => {
  it('separate repeated failed fetches stay on the bounded slow tier over a long outage', async () => {
    vi.spyOn(api, 'listAgentActivity').mockRejectedValue(new Error('offline'))
    const { wrapper } = createWrapper()
    renderHook(() => useAgentActivity(CLASS_ID, SESSION_ID, true), { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(59_500)
    })
    // Initial failure + one bounded 30s retry. The live 2s cadence would have made 30
    // more requests in this window.
    expect(vi.mocked(api.listAgentActivity)).toHaveBeenCalledTimes(2)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    // The next bounded retry lands, and the count stays slow while offline.
    expect(vi.mocked(api.listAgentActivity)).toHaveBeenCalledTimes(3)
    // Long outage: every retry is a separate failed fetch, and each one re-arms the same
    // 30s tier - six requests over ~150s where the live cadence would have made 75.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(90_000)
    })
    expect(vi.mocked(api.listAgentActivity).mock.calls.length).toBeLessThanOrEqual(6)
  })
})

// ── The work surface end to end: demand, hidden windows, unmount ────────────────────────

describe('the work surface (PLA-509 demand-driven observation)', () => {
  function counts() {
    return {
      activity: vi.mocked(api.listAgentActivity).mock.calls.length,
      dismissals: vi.mocked(api.listAgentAccessDismissals).mock.calls.length,
      changes: vi.mocked(api.listAgentWorkspaceChanges).mock.calls.length,
      commands: vi.mocked(api.listAgentCommands).mock.calls.length,
    }
  }

  it('a settled conversation (no workspace) is silent after its initial reads', async () => {
    const { wrapper } = createWrapper()
    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    // Before: 44 requests in this window (31 activity + 13 dismissals). Now: the two
    // initial reads and nothing else.
    expect(counts()).toEqual({ activity: 1, dismissals: 1, changes: 0, commands: 0 })
  })

  it('a settled conversation with an attached workspace is silent after its initial reads', async () => {
    vi.mocked(api.getAgentWorkspace).mockResolvedValue(workspace())
    const { wrapper } = createWrapper()
    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    // Before: 106 requests (31 activity + 13 dismissals + 31 changes + 31 commands).
    expect(counts()).toEqual({ activity: 1, dismissals: 1, changes: 1, commands: 1 })
  })

  it('pending approvals show their card without keeping a poll alive', async () => {
    // A workspace without the command grant: the run_commands request is unsatisfied, so
    // its card is live work - but a settled card is not a polling reason.
    vi.mocked(api.getAgentWorkspace).mockResolvedValue(workspace({ commands_enabled: false }))
    vi.mocked(api.listAgentActivity).mockResolvedValue([
      accessEvent({ target_id: 'run_commands', result_summary: { scope: 'run_commands' } }),
    ])
    const { wrapper } = createWrapper()
    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText('Prepare verification commands')).toBeInTheDocument()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(vi.mocked(api.listAgentActivity)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api.listAgentAccessDismissals)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api.listAgentWorkspaceChanges)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api.listAgentCommands)).toHaveBeenCalledTimes(1)
  })

  it('a running command keeps a bounded 2s observation, which stops when it settles', async () => {
    vi.mocked(api.getAgentWorkspace).mockResolvedValue(workspace())
    let commandState: AgentCommandRequestRead['state'] = 'running'
    const commands = vi
      .spyOn(api, 'listAgentCommands')
      .mockImplementation(async () => [runningCommand({ state: commandState })])
    const { wrapper } = createWrapper()
    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    // Phase-relative from here: the live observation runs at a 2s cadence.
    const atStart = commands.mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    // While the command is live: five 2s observations in ten seconds.
    expect(commands.mock.calls.length).toBe(atStart + 5)
    commandState = 'completed'
    await act(async () => {
      await vi.advanceTimersByTimeAsync(48_000)
    })
    // One more observation finds the command settled - and then nothing: the settled
    // workspace goes quiet instead of polling a completed row forever.
    expect(commands.mock.calls.length).toBe(atStart + 6)
  })

  it('a turn the surface started (retry) is observed only while it is actually in flight', async () => {
    vi.mocked(api.listMessages).mockResolvedValue([failedTurnMessage()])
    let resolveRetry: (() => void) | null = null
    vi.spyOn(api, 'retryAgentChat').mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveRetry = () =>
            resolve({
              message_id: 44,
              content: 'Done.',
              stopped: 'complete',
              detail: 'Complete.',
              activity: [],
              source_ids: [],
              workspace_change_ids: [],
              command_request_ids: [],
              profile_fact_ids: [],
            })
        }),
    )
    const { wrapper } = createWrapper()
    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    // Synchronous lookup: the initial reads have settled, so the failed-turn Retry is
    // on screen (findBy* would race the fake timers). The transcript-growth refresh seam
    // may have already reconciled once, so counts are phase-relative from here.
    const atStart = vi.mocked(api.listAgentActivity).mock.calls.length
    const retry = screen.getByRole('button', { name: 'Retry this turn' })
    act(() => {
      retry.click()
    })
    // The turn is in flight: the observation opens at the 2s cadence.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    expect(vi.mocked(api.listAgentActivity).mock.calls.length).toBe(atStart + 5)
    act(() => {
      resolveRetry?.()
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50_000)
    })
    // The turn's terminal invalidation refetches once; a settled turn with no live rows
    // asks nothing more.
    expect(vi.mocked(api.listAgentActivity).mock.calls.length).toBe(atStart + 6)
  })

  it('suspends the agent observation while hidden and reconciles on return', async () => {
    const activity = vi.mocked(api.listAgentActivity)
    const { wrapper } = createWrapper()
    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    expect(activity).toHaveBeenCalledTimes(1)

    setVisibility('hidden')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    // Genuinely hidden: no agent observation - the old code's interval kept asking
    // regardless (31 more requests in this window).
    expect(activity).toHaveBeenCalledTimes(1)

    setVisibility('visible')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    // Returning reconciles with one authoritative read per observation query.
    expect(activity).toHaveBeenCalledTimes(2)
    expect(vi.mocked(api.listAgentAccessDismissals)).toHaveBeenCalledTimes(2)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    // Settled and visible: quiet again.
    expect(activity).toHaveBeenCalledTimes(2)
    expect(vi.mocked(api.listAgentAccessDismissals)).toHaveBeenCalledTimes(2)
  })

  it('keeps a live job reconciled across a hide, and stops it when it settles hidden', async () => {
    vi.mocked(api.getAgentWorkspace).mockResolvedValue(workspace())
    const commands = vi.spyOn(api, 'listAgentCommands').mockResolvedValue([runningCommand()])
    const { wrapper } = createWrapper()
    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    expect(commands).toHaveBeenCalledTimes(1)

    setVisibility('hidden')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    // The job is still live, but a hidden window does not pay for the observation: the
    // interval ticks are skipped while hidden (pausing UI polling never cancels the
    // command itself - that runs server-side regardless).
    expect(commands).toHaveBeenCalledTimes(1)

    commands.mockResolvedValue([runningCommand({ state: 'completed', exit_code: 0 })])
    setVisibility('visible')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    // The window-focus reconciliation sees the completed command - exactly what a
    // hidden-paused poll would have missed - and the settled workspace goes quiet.
    expect(commands).toHaveBeenCalledTimes(2)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    expect(commands).toHaveBeenCalledTimes(2)
  })

  it('rapid hide/show reconciles once per return without multiplying timers', async () => {
    const activity = vi.mocked(api.listAgentActivity)
    const { wrapper } = createWrapper()
    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    expect(activity).toHaveBeenCalledTimes(1)

    for (let i = 0; i < 10; i++) {
      setVisibility('hidden')
      setVisibility('visible')
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000)
      })
    }
    // One reconciliation read per return the student actually made - linear, not
    // multiplicative.
    expect(activity).toHaveBeenCalledTimes(11)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    // No accumulated timers: a quiet window stays quiet.
    expect(activity).toHaveBeenCalledTimes(11)
  })

  it('unmount retires every observation: no requests after the surface is gone', async () => {
    const { wrapper } = createWrapper()
    const rendered = render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, {
      wrapper,
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    const atUnmount = vi.mocked(api.listAgentActivity).mock.calls.length
    rendered.unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(vi.mocked(api.listAgentActivity).mock.calls.length).toBe(atUnmount)
  })
})

// ── Requested background jobs keep their observation while hidden (PLA-509 acceptance 3) ─

describe('background job observation: hidden pauses the UI, return reconciles', () => {
  it('pauses a document status observation while hidden and reconciles once on return', async () => {
    const status = vi
      .spyOn(api, 'getDocumentStatus')
      .mockResolvedValue(documentStatus('extracting'))
    const { wrapper } = createWrapper()
    renderHook(() => useDocumentStatus(1), { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000)
    })
    // Visible window: the live backoff (500ms to 2s) asks while the batch runs.
    expect(status.mock.calls.length).toBeGreaterThanOrEqual(3)
    // A visible-but-unfocused window keeps observing: the focus manager reads the
    // platform's visibility state, not window focus - blur/focus change nothing.
    window.dispatchEvent(new Event('blur'))
    window.dispatchEvent(new Event('focus'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000)
    })
    expect(status.mock.calls.length).toBeGreaterThan(3)

    // Hidden window: the UI stops asking. No request is sent while hidden, so there is
    // nothing to cancel - the server-side ingestion is untouched; the pause only stops
    // the re-asking.
    setVisibility('hidden')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    // The visible window paid a bounded backoff (a handful of checks, not a flood); the
    // hidden window paid nothing.
    const atHide = status.mock.calls.length
    expect(atHide).toBeGreaterThanOrEqual(5)
    expect(atHide).toBeLessThanOrEqual(8)

    // The batch finished while away. Return: exactly one authoritative read reconciles
    // the stage the batch reached, and the terminal state ends the observation.
    status.mockResolvedValue(documentStatus('ready'))
    setVisibility('visible')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(status.mock.calls.length).toBe(atHide + 1)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000)
    })
    expect(status.mock.calls.length).toBe(atHide + 1)
  })

  it('pauses a draft pass observation while hidden and resumes it on return', async () => {
    const list = vi.spyOn(api, 'listDrafts').mockResolvedValue([
      { id: 1, title: 'Essay', state: 'generating' } as never,
    ])
    const { wrapper } = createWrapper()
    renderHook(() => useDrafts(CLASS_ID), { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000)
    })
    // The pass polls at its 1500ms demand cadence while the window is visible.
    expect(list.mock.calls.length).toBeGreaterThanOrEqual(3)
    setVisibility('hidden')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000)
    })
    // No asks while hidden - the pass keeps running on the server, uncancelled.
    const atHide = list.mock.calls.length
    expect(atHide).toBeLessThanOrEqual(5)
    setVisibility('visible')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(list.mock.calls.length).toBe(atHide + 1)
    // The pass is still generating: the observation resumes on the live cadence.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000)
    })
    expect(list.mock.calls.length).toBeGreaterThanOrEqual(atHide + 2)
  })

  it('pauses study generation observation while hidden and resumes it on return', async () => {
    const list = vi
      .spyOn(api, 'listStudy')
      .mockResolvedValue({
        decks: [{ id: 1, title: 'Ch. 1', state: 'generating' }],
        quizzes: [],
      } as unknown as StudyListRead)
    const { wrapper } = createWrapper()
    renderHook(() => useStudyList(CLASS_ID), { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000)
    })
    expect(list.mock.calls.length).toBeGreaterThanOrEqual(2)
    setVisibility('hidden')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000)
    })
    const atHide = list.mock.calls.length
    expect(atHide).toBeLessThanOrEqual(5)
    setVisibility('visible')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(list.mock.calls.length).toBe(atHide + 1)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000)
    })
    expect(list.mock.calls.length).toBeGreaterThanOrEqual(atHide + 2)
  })

  it('backs off to the bounded slow tier when the read itself fails', async () => {
    vi.spyOn(api, 'getDocumentStatus').mockRejectedValue(new Error('offline'))
    const { wrapper } = createWrapper()
    renderHook(() => useDocumentStatus(1), { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(25_000)
    })
    // The failure must not be retried at the live cadence: one slow check, not dozens.
    expect(vi.mocked(api.getDocumentStatus)).toHaveBeenCalledTimes(1)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    expect(vi.mocked(api.getDocumentStatus)).toHaveBeenCalledTimes(2)
  })

  it('does not retry an empty result rapidly (no-data paths stay quiet)', async () => {
    const drafts = vi.spyOn(api, 'listDrafts').mockResolvedValue([])
    const documents = vi.spyOn(api, 'listDocuments').mockResolvedValue([])
    const { wrapper } = createWrapper()
    renderHook(() => useDrafts(CLASS_ID), { wrapper })
    renderHook(() => useDocuments(CLASS_ID), { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    expect(drafts).toHaveBeenCalledTimes(1)
    expect(documents).toHaveBeenCalledTimes(1)
  })
})

// ── Turn-lifecycle observation (PLA-509 cross-lane signal) ──────────────────────────────
//
// The chat pane announces its own turns through the owner-scoped helpers and the work
// surface observes from the QueryClient cache. These tests pin the observation contract:
// an announced turn is observed from an empty cache, every owner is released by its own
// terminal path, and a stale release cannot close a live turn.

describe('turn-lifecycle observation (PLA-509 cross-lane signal)', () => {
  function changeRow(state: AgentWorkspaceChangeRead['state']): AgentWorkspaceChangeRead {
    return {
      id: 2,
      workspace_id: 1,
      session_id: SESSION_ID,
      path: 'src/parser.py',
      rationale: 'Fix the edge case',
      state,
      current_hash: 'abc123',
      current_content: 'x',
      proposed_content: 'y',
      hunks: [],
      created_at: '2026-09-02T00:00:00Z',
      updated_at: '2026-09-02T00:00:01Z',
    }
  }

  it('pending and partially-applied proposals do not keep a poll alive', async () => {
    vi.mocked(api.getAgentWorkspace).mockResolvedValue(workspace())
    vi.mocked(api.listAgentWorkspaceChanges).mockResolvedValue([
      changeRow('pending'),
      changeRow('partially_applied'),
    ])
    const { wrapper } = createWrapper()
    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    // Open but settled decisions: they move when the student acts (the approving mutation
    // invalidates) or a turn runs (the turn signal goes live) - never by watching.
    expect(vi.mocked(api.listAgentActivity)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api.listAgentWorkspaceChanges)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api.listAgentCommands)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api.listAgentAccessDismissals)).toHaveBeenCalledTimes(1)
  })

  it('observes an announced turn from an empty cache, and its terminal path stops it', async () => {
    const activity = vi.mocked(api.listAgentActivity)
    const { queryClient, wrapper } = createWrapper()
    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    expect(activity).toHaveBeenCalledTimes(1)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    // Settled conversation, empty cache: silence.
    expect(activity).toHaveBeenCalledTimes(1)

    // The pane announces the turn with a unique owner (e.g. its message id). No cache row
    // exists yet - only the signal can open the observation.
    await act(async () => {
      beginAgentTurnObservation(queryClient, CLASS_ID, SESSION_ID, 'msg-77')
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    expect(activity.mock.calls.length).toBe(1 + 5)

    // The turn's own terminal path (success or failure) releases exactly its owner, and
    // the surface goes quiet again.
    await act(async () => {
      endAgentTurnObservation(queryClient, CLASS_ID, SESSION_ID, 'msg-77')
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    expect(activity.mock.calls.length).toBe(6)
  })

  it('useAgentTurnsLive reflects the registry, owner by owner', async () => {
    const { queryClient, wrapper } = createWrapper()
    const { result } = renderHook(() => useAgentTurnsLive(CLASS_ID, SESSION_ID), { wrapper })
    expect(result.current).toBe(false)
    // Let the one-time seed fetch settle before announcing. (`setQueryData` schedules the
    // observer's re-render on a zero-delay timer, so under fake timers each announcement
    // needs the zero-delay flush before the new result is visible.)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    await act(async () => {
      beginAgentTurnObservation(queryClient, CLASS_ID, SESSION_ID, 'msg-1')
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current).toBe(true)
    await act(async () => {
      endAgentTurnObservation(queryClient, CLASS_ID, SESSION_ID, 'msg-2')
      await vi.advanceTimersByTimeAsync(0)
    })
    // Releasing an unknown owner does not clear a live one.
    expect(result.current).toBe(true)
    await act(async () => {
      endAgentTurnObservation(queryClient, CLASS_ID, SESSION_ID, 'msg-1')
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current).toBe(false)
  })

  it('releases are owner-scoped: a stale completion cannot close a newer turn', async () => {
    const activity = vi.mocked(api.listAgentActivity)
    const { queryClient, wrapper } = createWrapper()
    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })

    await act(async () => {
      beginAgentTurnObservation(queryClient, CLASS_ID, SESSION_ID, 'msg-77')
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    expect(activity.mock.calls.length).toBe(1 + 5)

    // While the first turn is still live, a second starts (a follow-up, a retry).
    await act(async () => {
      beginAgentTurnObservation(queryClient, CLASS_ID, SESSION_ID, 'msg-78')
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    expect(activity.mock.calls.length).toBe(6 + 5)

    // The older turn's late completion arrives: it releases only its own owner, so the
    // newer turn keeps being observed.
    await act(async () => {
      endAgentTurnObservation(queryClient, CLASS_ID, SESSION_ID, 'msg-77')
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    expect(activity.mock.calls.length).toBe(11 + 5)

    // A release for an unknown owner is a no-op: a duplicate or lost release never
    // disturbs a live turn.
    await act(async () => {
      endAgentTurnObservation(queryClient, CLASS_ID, SESSION_ID, 'msg-99')
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    expect(activity.mock.calls.length).toBe(16 + 5)

    // The newer turn's terminal path closes the observation.
    await act(async () => {
      endAgentTurnObservation(queryClient, CLASS_ID, SESSION_ID, 'msg-78')
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    expect(activity.mock.calls.length).toBe(21)
  })
})

describe('query key stability under the new signatures', () => {
  it('the observation keys are unchanged, so cached work survives the hook rewire', () => {
    expect(agentKeys.activity(CLASS_ID, SESSION_ID)).toEqual([
      'agent',
      'activity',
      CLASS_ID,
      SESSION_ID,
    ])
    expect(agentKeys.changes(CLASS_ID, SESSION_ID)).toEqual([
      'agent',
      'changes',
      CLASS_ID,
      SESSION_ID,
    ])
    expect(agentKeys.commands(CLASS_ID, SESSION_ID)).toEqual([
      'agent',
      'commands',
      CLASS_ID,
      SESSION_ID,
    ])
    expect(agentKeys.dismissals(CLASS_ID, SESSION_ID)).toEqual([
      'agent',
      'dismissals',
      CLASS_ID,
      SESSION_ID,
    ])
  })
})
