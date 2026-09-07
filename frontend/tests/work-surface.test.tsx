import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { AgentWorkSurface } from '@/components/agent/work-surface'
import { WorkspaceAttachProvider, WorkspaceContextChip } from '@/components/agent/workspace-attach'
import { TooltipProvider } from '@/components/ui/tooltip'
import { api } from '@/lib/api'
import { chatKeys } from '@/lib/hooks/use-chat'
import { agentKeys } from '@/lib/hooks/use-agent'
import * as runtime from '@/lib/runtime'

// The Details audit embeds the class source ledger, which needs the app router. These
// tests exercise the work surface itself, so stub the ledger rather than stand up routing.
vi.mock('@/components/drafts/source-ledger', () => ({
  SourceLedger: () => null,
}))
import type {
  AgentAuditEventRead,
  AgentCommandRequestRead,
  AgentWorkspaceChangeRead,
  MessageRead,
} from '@/types'

const CLASS_ID = 9
const SESSION_ID = 11

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      {/* The app wraps every route in one tooltip provider; the tests mirror that seam. */}
      <TooltipProvider delayDuration={300}>
        <WorkspaceAttachProvider classId={CLASS_ID}>{children}</WorkspaceAttachProvider>
      </TooltipProvider>
    </QueryClientProvider>
  )
  return { queryClient, wrapper }
}

function accessEvent(overrides: Partial<AgentAuditEventRead>): AgentAuditEventRead {
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

function workspace(overrides: Partial<Record<string, boolean>> = {}) {
  return {
    id: 1,
    class_id: CLASS_ID,
    root_path: '/tmp/proj',
    display_name: 'proj',
    read_enabled: true,
    change_proposals_enabled: false,
    commands_enabled: false,
    created_at: '2026-09-02T00:00:00Z',
    updated_at: '2026-09-02T00:00:00Z',
    ...overrides,
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(runtime, 'desktopFolderPickerAvailable').mockReturnValue(false)
  vi.spyOn(runtime, 'pickDesktopWorkspaceDirectory').mockResolvedValue(null)
  vi.spyOn(api, 'getAgentWorkspace').mockResolvedValue(null)
  vi.spyOn(api, 'listAgentActivity').mockResolvedValue([])
  vi.spyOn(api, 'listAgentAccessDismissals').mockResolvedValue({ dismissals: [] })
  vi.spyOn(api, 'listAgentWorkspaceChanges').mockResolvedValue([])
  vi.spyOn(api, 'listAgentCommands').mockResolvedValue([])
  vi.spyOn(api, 'listMessages').mockResolvedValue([])
})

describe('the contextual agent work surface (PLA-401)', () => {
  it('keeps a failed folder path and its error available for correction and retry', async () => {
    const attach = vi
      .spyOn(api, 'attachAgentWorkspace')
      .mockRejectedValueOnce(new Error('Folder not found'))
      .mockResolvedValue(workspace())
    const { wrapper } = createWrapper()
    render(
      <>
        <AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />
        <WorkspaceContextChip />
      </>,
      { wrapper },
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Attach a folder' }))
    const input = await screen.findByLabelText('Path to the folder')
    fireEvent.change(input, { target: { value: '/tmp/starter' } })
    fireEvent.click(screen.getByRole('button', { name: 'Attach' }))
    expect(await screen.findByText('Folder not found')).toBeInTheDocument()
    expect(input).toHaveValue('/tmp/starter')
    fireEvent.click(screen.getByRole('button', { name: 'Attach' }))
    expect(await screen.findByText('Workspace: proj')).toBeInTheDocument()
    expect(attach).toHaveBeenCalledTimes(2)
  })

  it.each([
    ['listAgentActivity', 'Activity'],
    ['listAgentWorkspaceChanges', 'File proposals'],
    ['listAgentCommands', 'Commands'],
    ['listAgentAccessDismissals', 'Access requests'],
  ] as const)('shows a recoverable failure for %s', async (method, label) => {
    vi.mocked(api.getAgentWorkspace).mockResolvedValue(workspace())
    vi.mocked(api[method]).mockRejectedValueOnce(new Error('offline'))
    const { wrapper } = createWrapper()
    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })
    expect(await screen.findByText(`${label} could not be loaded`)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: `Retry ${label.toLowerCase()}` }))
    await waitFor(() =>
      expect(screen.queryByText(`${label} could not be loaded`)).not.toBeInTheDocument(),
    )
  })

  it('adopts live stale review even though the polled list only returns stored metadata', async () => {
    vi.mocked(api.getAgentWorkspace).mockResolvedValue(workspace())
    const stored = change({ current_content: null, proposed_content: null })
    vi.mocked(api.listAgentWorkspaceChanges).mockResolvedValue([stored])
    const review = vi
      .spyOn(api, 'reviewAgentWorkspaceChange')
      .mockResolvedValue(change({ state: 'stale', current_content: 'print("external edit")' }))
    const confirm = vi.spyOn(api, 'confirmAgentWorkspaceChange')
    const reject = vi
      .spyOn(api, 'rejectAgentWorkspaceChange')
      .mockResolvedValue({} as Awaited<ReturnType<typeof api.rejectAgentWorkspaceChange>>)
    const { wrapper, queryClient } = createWrapper()
    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })
    await userEvent.click(await screen.findByRole('button', { name: 'Accept remaining' }))
    expect(await screen.findByText('Proposal is stale')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Current file' })).toHaveTextContent(
      'print("external edit")',
    )
    expect(screen.getByRole('region', { name: 'Proposed file' })).toHaveTextContent(
      'print("after")',
    )
    expect(confirm).not.toHaveBeenCalled()
    await act(() =>
      queryClient.invalidateQueries({ queryKey: agentKeys.changes(CLASS_ID, SESSION_ID) }),
    )
    expect(screen.getByRole('region', { name: 'Current file' })).toHaveTextContent(
      'print("external edit")',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Recheck file' }))
    expect(review).toHaveBeenCalledTimes(2)
    await userEvent.click(screen.getByRole('button', { name: 'Reject proposal' }))
    await waitFor(() => expect(reject).toHaveBeenCalledWith(CLASS_ID, SESSION_ID, 1))
  })

  it('blocks cached failed-turn retry until history refresh succeeds', async () => {
    const cached = [
      message({
        agent_attempt: { state: 'failed', stopped_reason: 'failed', detail: 'Try again' },
      }),
    ]
    vi.mocked(api.listMessages)
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue(cached)
    const retry = vi.spyOn(api, 'retryAgentChat')
    const { wrapper, queryClient } = createWrapper()
    queryClient.setQueryData(chatKeys.messages(SESSION_ID), cached)
    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })
    await waitFor(() =>
      expect(queryClient.getQueryState(chatKeys.messages(SESSION_ID))?.status).toBe('error'),
    )
    expect(screen.getByRole('button', { name: 'Retry this turn' })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: 'Retry this turn' }))
    expect(retry).not.toHaveBeenCalled()
    await act(() => queryClient.invalidateQueries({ queryKey: chatKeys.messages(SESSION_ID) }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Retry this turn' })).toBeEnabled(),
    )
  })

  it('defers access continuation while cached activity cannot be refreshed', async () => {
    const transcript = [
      message({
        agent_attempt: { state: 'stopped', stopped_reason: null, detail: 'Waiting for access' },
      }),
      message({
        id: 2,
        role: 'assistant',
        content: 'Attach the folder first.',
        created_at: '2026-09-02T00:00:02Z',
      }),
    ]
    const events = [accessEvent({ target_id: 'attach', result_summary: { scope: 'attach' } })]
    vi.mocked(api.listMessages).mockResolvedValue(transcript)
    vi.mocked(api.listAgentActivity).mockRejectedValue(new Error('offline'))
    vi.spyOn(api, 'attachAgentWorkspace').mockResolvedValue(workspace())
    const regenerate = vi.spyOn(api, 'regenerateAgentChat').mockResolvedValue({
      message_id: 2,
      content: 'Read it.',
      stopped: '',
      detail: '',
      activity: [],
      source_ids: [],
      profile_fact_ids: [],
      workspace_change_ids: [],
      command_request_ids: [],
    })
    const { wrapper, queryClient } = createWrapper()
    queryClient.setQueryData(chatKeys.messages(SESSION_ID), transcript)
    queryClient.setQueryData(agentKeys.activity(CLASS_ID, SESSION_ID), events)
    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })
    expect(await screen.findByText('Activity could not be loaded')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Attach a folder' }))
    fireEvent.change(await screen.findByLabelText('Path to the folder'), {
      target: { value: '/tmp/starter' },
    })
    await userEvent.click(screen.getByRole('button', { name: 'Attach' }))
    await waitFor(() => expect(api.attachAgentWorkspace).toHaveBeenCalled())
    await waitFor(() =>
      expect(screen.queryByLabelText('Path to the folder')).not.toBeInTheDocument(),
    )
    expect(regenerate).not.toHaveBeenCalled()
    vi.mocked(api.listAgentActivity).mockResolvedValue(events)
    await act(() =>
      queryClient.invalidateQueries({ queryKey: agentKeys.activity(CLASS_ID, SESSION_ID) }),
    )
    await waitFor(() => expect(regenerate).toHaveBeenCalledTimes(1))
  })

  it('asks for missing access as one compact card, and approving uses the ordinary grant path', async () => {
    vi.spyOn(api, 'getAgentWorkspace').mockResolvedValue(workspace({ read_enabled: false }))
    vi.spyOn(api, 'listAgentActivity').mockResolvedValue([accessEvent({ target_id: 'read' })])
    const update = vi.spyOn(api, 'updateAgentWorkspaceGrants').mockResolvedValue(workspace())
    const { wrapper } = createWrapper()
    const attachSpy = vi.spyOn(api, 'attachAgentWorkspace')
    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })

    const card = await screen.findByText('Read the attached folder')
    expect(card).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(update).toHaveBeenCalledWith(CLASS_ID, { read_enabled: true }))
    // The safety model is untouched: the grant goes through the audited grants endpoint,
    // not a new effect path.
    expect(attachSpy).not.toHaveBeenCalled()
  })

  it('approving a change proposal enables the minimum grant pair when read is still off', async () => {
    vi.spyOn(api, 'getAgentWorkspace').mockResolvedValue(
      workspace({ read_enabled: false, change_proposals_enabled: false }),
    )
    vi.spyOn(api, 'listAgentActivity').mockResolvedValue([
      accessEvent({ target_id: 'propose_changes', id: 'ev-2' }),
    ])
    const update = vi.spyOn(api, 'updateAgentWorkspaceGrants').mockResolvedValue(workspace())
    const { wrapper } = createWrapper()

    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })

    await screen.findByText('Prepare file edits')
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() =>
      expect(update).toHaveBeenCalledWith(CLASS_ID, {
        change_proposals_enabled: true,
        read_enabled: true,
      }),
    )
  })

  it('offers attach from the composer chip, with a bounded path entry in the surface when no picker exists', async () => {
    // No workspace attached: the chip beside the composer is the attach affordance, and
    // the bounded path entry appears in the conversation surface - not as setup chrome.
    const attach = vi.spyOn(api, 'attachAgentWorkspace').mockResolvedValue(workspace())
    const { wrapper } = createWrapper()

    render(
      <>
        <AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />
        <WorkspaceContextChip />
      </>,
      { wrapper },
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Attach a folder' }))
    // No native picker in the test environment: the bounded path entry stands in.
    const input = await screen.findByLabelText('Path to the folder')
    fireEvent.change(input, { target: { value: '/tmp/starter' } })
    fireEvent.click(screen.getByRole('button', { name: 'Attach' }))
    await waitFor(() =>
      expect(attach).toHaveBeenCalledWith(CLASS_ID, '/tmp/starter', {
        displayName: undefined,
        readEnabled: true,
      }),
    )
    // The chip becomes the attached-workspace chip in the same place it started.
    await screen.findByText('Workspace: proj')
  })

  it('shows the attached workspace as a compact chip beside the composer, and detaches from it', async () => {
    vi.spyOn(api, 'getAgentWorkspace').mockResolvedValue(workspace())
    const detach = vi.spyOn(api, 'detachAgentWorkspace').mockResolvedValue(undefined)
    const { wrapper } = createWrapper()

    render(
      <>
        <AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />
        <WorkspaceContextChip />
      </>,
      { wrapper },
    )

    // Same weight as the source-context chip beside it: a compact context affordance,
    // with no dashboard or setup section anywhere in the surface.
    const chip = await screen.findByText('Workspace: proj')
    expect(chip.closest('[data-workspace-chip]')).not.toBeNull()
    expect(screen.queryByRole('button', { name: /attach a folder/i })).toBeNull()

    await userEvent.click(screen.getByRole('button', { name: 'Workspace options' }))
    await userEvent.click(await screen.findByText('Detach workspace'))
    await waitFor(() => expect(detach).toHaveBeenCalledWith(CLASS_ID))
    await screen.findByRole('button', { name: 'Attach a folder' })
  })

  it('shows no access cards once every requested scope is granted', async () => {
    vi.spyOn(api, 'getAgentWorkspace').mockResolvedValue(
      workspace({ change_proposals_enabled: true, commands_enabled: true }),
    )
    vi.spyOn(api, 'listAgentActivity').mockResolvedValue([
      accessEvent({ target_id: 'read', id: 'ev-1' }),
      accessEvent({ target_id: 'propose_changes', id: 'ev-2' }),
      accessEvent({ target_id: 'run_commands', id: 'ev-3' }),
    ])
    const { wrapper } = createWrapper()

    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })

    // All three scopes are granted, so the cards are derived away: no dashboard.
    expect(screen.queryByText('Read the attached folder')).toBeNull()
    expect(screen.queryByText('Prepare file edits')).toBeNull()
    expect(screen.queryByText('Prepare verification commands')).toBeNull()
  })

  it('hides a dismissed request for the rest of the session without granting it', async () => {
    vi.spyOn(api, 'getAgentWorkspace').mockResolvedValue(workspace({ read_enabled: false }))
    vi.spyOn(api, 'listAgentActivity').mockResolvedValue([accessEvent({ target_id: 'read' })])
    vi.spyOn(api, 'updateAgentWorkspaceGrants').mockResolvedValue(workspace())
    const dismiss = vi
      .spyOn(api, 'dismissAgentAccess')
      .mockResolvedValue({ scope: 'read', dismissed_at: '2026-09-02T00:00:00Z' })
    const regenerate = vi.spyOn(api, 'regenerateAgentChat')
    const active: string[] = []
    vi.spyOn(api, 'listAgentAccessDismissals').mockImplementation(async () => ({
      dismissals: active.map((scope) => ({ scope, dismissed_at: '2026-09-02T00:00:00Z' })),
    }))
    const { wrapper } = createWrapper()

    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })

    await screen.findByText('Read the attached folder')
    fireEvent.click(screen.getByRole('button', { name: 'Not now' }))
    // "Not now" is server state with a bounded lifetime, not component-local state: the
    // dismissal is recorded against the conversation, so a reload or unmount cannot
    // resurface the card while it is active.
    active.push('read')
    await waitFor(() => expect(dismiss).toHaveBeenCalledWith(CLASS_ID, SESSION_ID, 'read'))
    await waitFor(() => expect(screen.queryByText('Read the attached folder')).toBeNull())
    // Dismissing is not a resolution: nothing is granted and nothing is re-run.
    expect(api.updateAgentWorkspaceGrants).not.toHaveBeenCalled()
    expect(regenerate).not.toHaveBeenCalled()
  })

  it('re-answers the interrupted turn once the access its request asked for is resolved', async () => {
    vi.spyOn(api, 'listAgentActivity').mockResolvedValue([
      accessEvent({
        target_id: 'attach',
        result_summary: {
          scope: 'attach',
          reason: 'To inspect this starter project, Lyra needs to open the folder.',
        },
      }),
    ])
    vi.spyOn(api, 'listMessages').mockResolvedValue([
      message({
        agent_attempt: {
          state: 'stopped',
          stopped_reason: null,
          detail: 'Waiting for folder access',
        },
      }),
      message({
        id: 2,
        role: 'assistant',
        content: 'I need access to the folder first.',
        created_at: '2026-09-02T00:00:02Z',
      }),
    ])
    vi.spyOn(api, 'attachAgentWorkspace').mockResolvedValue(workspace())
    const regenerate = vi.spyOn(api, 'regenerateAgentChat').mockResolvedValue({
      message_id: 2,
      content: 'It is a two-file project: main.py drives parser.py.',
      stopped: '',
      detail: '',
      activity: [],
      source_ids: [],
      profile_fact_ids: [],
      workspace_change_ids: [],
      command_request_ids: [],
    })
    const { wrapper } = createWrapper()

    render(
      <>
        <AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />
        <WorkspaceContextChip />
      </>,
      { wrapper },
    )

    await screen.findByText('To inspect this starter project, Lyra needs to open the folder.')
    // The card carries the attach verb; the composer's icon shares the name, so scope to
    // the card that asked for the folder.
    const attachCard = document.querySelector('[data-access-request="attach"]')
    if (!attachCard) throw new Error('expected the attach-scope access card')
    fireEvent.click(
      within(attachCard as HTMLElement).getByRole('button', { name: 'Attach a folder' }),
    )
    const input = await screen.findByLabelText('Path to the folder')
    fireEvent.change(input, { target: { value: '/tmp/starter' } })
    fireEvent.click(screen.getByRole('button', { name: 'Attach' }))
    await waitFor(() => expect(api.attachAgentWorkspace).toHaveBeenCalled())
    // The student's turn is continued for them: the same question is re-answered with the
    // access now in hand, instead of asking for it again.
    await waitFor(() => expect(regenerate).toHaveBeenCalledWith(CLASS_ID, SESSION_ID))
  })

  it('does not re-run the turn while another scope still needs its own review', async () => {
    vi.spyOn(api, 'listAgentActivity').mockResolvedValue([
      accessEvent({
        target_id: 'attach',
        result_summary: {
          scope: 'attach',
          reason: 'To inspect this starter project, Lyra needs to open the folder.',
        },
      }),
      accessEvent({
        id: 'ev-2',
        target_id: 'propose_changes',
        result_summary: {
          scope: 'propose_changes',
          reason: 'To add the parser skeleton, Lyra needs to edit files.',
        },
      }),
    ])
    vi.spyOn(api, 'listMessages').mockResolvedValue([
      message({
        agent_attempt: { state: 'stopped', stopped_reason: null, detail: 'Waiting for access' },
      }),
      message({
        id: 2,
        role: 'assistant',
        content: 'I need access first.',
        created_at: '2026-09-02T00:00:02Z',
      }),
    ])
    vi.spyOn(api, 'attachAgentWorkspace').mockResolvedValue(workspace())
    const regenerate = vi.spyOn(api, 'regenerateAgentChat')
    const { wrapper } = createWrapper()

    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })

    const reason = await screen.findByText('To add the parser skeleton, Lyra needs to edit files.')
    const scope = reason.closest('[data-access-request]')
    if (!scope) throw new Error('expected the edit-scope access card')
    fireEvent.click(within(scope as HTMLElement).getByRole('button', { name: /not now/i }))
    const dismiss = vi.spyOn(api, 'dismissAgentAccess')
    await waitFor(() =>
      expect(dismiss).toHaveBeenCalledWith(CLASS_ID, SESSION_ID, 'propose_changes'),
    )
    expect(regenerate).not.toHaveBeenCalled()
    expect(api.attachAgentWorkspace).not.toHaveBeenCalled()
  })

  it('shows the task-specific reason and what still needs separate review', async () => {
    vi.spyOn(api, 'getAgentWorkspace').mockResolvedValue(workspace({ read_enabled: false }))
    vi.spyOn(api, 'listAgentActivity').mockResolvedValue([
      accessEvent({
        target_id: 'read',
        result_summary: {
          scope: 'read',
          reason: 'To understand this starter project, Lyra needs to read the files in proj.',
        },
      }),
    ])
    const { wrapper } = createWrapper()

    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })

    // The model's reason is the card's main line - not a generic status phrase.
    expect(
      await screen.findByText(
        'To understand this starter project, Lyra needs to read the files in proj.',
      ),
    ).toBeInTheDocument()
    // What granting enables, and what still needs its own review.
    expect(
      screen.getByText(/Lyra can list and read the text files in the folder\./),
    ).toBeInTheDocument()
    expect(screen.getByText(/each still need their own approval/)).toBeInTheDocument()
  })

  it('surfaces failed command results without opening settled history', async () => {
    vi.spyOn(api, 'getAgentWorkspace').mockResolvedValue(workspace())
    vi.spyOn(api, 'listAgentCommands').mockResolvedValue([
      command({ state: 'failed', exit_code: 1, stderr_text: 'Required fixture is missing' }),
    ])
    const { wrapper } = createWrapper()
    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })
    const failure = await screen.findByText('1 result needs attention')
    expect(failure).toBeVisible()
    fireEvent.click(failure)
    expect(await screen.findByText('Required fixture is missing')).toBeVisible()
  })

  it('keeps successful activity history compact and removes routine event counts', async () => {
    vi.spyOn(api, 'getAgentWorkspace').mockResolvedValue(workspace())
    vi.spyOn(api, 'listAgentActivity').mockResolvedValue([
      accessEvent({ tool: 'read_file', target_kind: 'file', target_id: 'main.py' }),
    ])
    const { wrapper } = createWrapper()
    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })
    expect(await screen.findByRole('button', { name: 'Activity history' })).toBeVisible()
    expect(screen.queryByText(/activity events/)).not.toBeInTheDocument()
  })

  it('keeps terminal work out of the primary band', async () => {
    // The list endpoints return every row in scope, so a finished change (applied or
    // rejected) or a settled command (completed or rejected) is history, not an
    // outstanding request. Only live work - a pending or partially-applied edit, a
    // pending or running command - belongs in the band the student is asked to act on.
    vi.spyOn(api, 'getAgentWorkspace').mockResolvedValue(workspace())
    vi.spyOn(api, 'listAgentWorkspaceChanges').mockResolvedValue([
      change({ id: 1, state: 'applied', path: 'main.py' }),
      change({ id: 2, state: 'rejected', path: 'util.py' }),
      change({ id: 3, state: 'pending', path: 'lib.py', rationale: 'Add the new helper.' }),
    ])
    vi.spyOn(api, 'listAgentCommands').mockResolvedValue([
      command({ id: 1, state: 'completed', argv: ['pytest'] }),
      command({ id: 2, state: 'pending', argv: ['python', 'run.py'] }),
    ])
    const { wrapper } = createWrapper()

    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })

    // The live work is present and actionable.
    expect(await screen.findByText('lib.py')).toBeInTheDocument()
    expect(screen.getByText('Add the new helper.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Confirm and run' })).toBeInTheDocument()
    // The terminal rows are not surfaced as outstanding work in the band.
    expect(screen.queryByText('main.py')).not.toBeInTheDocument()
    expect(screen.queryByText('util.py')).not.toBeInTheDocument()
    expect(screen.queryAllByText('pytest')).toHaveLength(0)
    // ...but they are not lost: the collapsed Details/audit carries them as settled
    // results, findable without being actionable.
    const details = screen.getByRole('button', { name: /Activity history/i })
    fireEvent.click(details)
    expect(await screen.findByText('main.py')).toBeInTheDocument()
    expect(screen.getByText('Applied', { exact: true })).toBeInTheDocument()
    expect(screen.getByText('util.py')).toBeInTheDocument()
    expect(screen.getByText('Rejected', { exact: true })).toBeInTheDocument()
    expect(screen.getByText('pytest')).toBeInTheDocument()
    expect(screen.getByText('Completed', { exact: true })).toBeInTheDocument()
  })

  it('keeps settled-only history as a collapsed audit, not as live band work', async () => {
    // With every edit and command settled, the primary band carries nothing actionable:
    // the surface remains only to host the collapsed Details audit (where the settled
    // results live), not to present finished work as an action the student still owes.
    vi.spyOn(api, 'getAgentWorkspace').mockResolvedValue(workspace())
    vi.spyOn(api, 'listAgentWorkspaceChanges').mockResolvedValue([
      change({ id: 1, state: 'applied', path: 'main.py' }),
      change({ id: 2, state: 'rejected', path: 'util.py' }),
    ])
    vi.spyOn(api, 'listAgentCommands').mockResolvedValue([
      command({ id: 1, state: 'completed', argv: ['pytest'] }),
      command({ id: 2, state: 'rejected', argv: ['pytest', '-x'] }),
    ])
    const { wrapper } = createWrapper()

    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })

    await waitFor(() => expect(api.listAgentWorkspaceChanges).toHaveBeenCalled())
    // The collapsed audit strip is present...
    const details = screen.getByRole('button', { name: /Activity history/i })
    expect(details).toBeInTheDocument()
    // ...with nothing live in the band: no actions, and the settled rows stay
    // inside the collapsed section.
    expect(screen.queryByRole('button', { name: /Accept remaining/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Confirm and run' })).not.toBeInTheDocument()
    expect(screen.queryByText('main.py')).not.toBeInTheDocument()
    expect(screen.queryByText('util.py')).not.toBeInTheDocument()
    // Expanding the audit shows the settled results, read-only.
    fireEvent.click(details)
    expect(await screen.findByText('main.py')).toBeInTheDocument()
    expect(screen.getByText('Applied', { exact: true })).toBeInTheDocument()
    expect(screen.getByText('pytest')).toBeInTheDocument()
    expect(screen.getByText('Completed', { exact: true })).toBeInTheDocument()
  })
})

function change(overrides: Partial<AgentWorkspaceChangeRead>): AgentWorkspaceChangeRead {
  return {
    id: 1,
    workspace_id: 1,
    session_id: SESSION_ID,
    path: 'main.py',
    rationale: null,
    state: 'pending',
    current_hash: 'hash-main',
    current_content: 'print("before")',
    proposed_content: 'print("after")',
    hunks: [{ index: 1, hash: 'hunk-1', lines: ['-print("before")', '+print("after")'] }],
    created_at: '2026-09-02T00:00:00Z',
    updated_at: '2026-09-02T00:00:00Z',
    ...overrides,
  }
}

function command(overrides: Partial<AgentCommandRequestRead>): AgentCommandRequestRead {
  return {
    id: 1,
    workspace_id: 1,
    session_id: SESSION_ID,
    argv: ['pytest'],
    relative_cwd: '.',
    reason: 'Verify the change.',
    expected_signal: null,
    timeout_seconds: 60,
    state: 'pending',
    confirmed_at: null,
    exit_code: null,
    stdout_text: null,
    stderr_text: null,
    truncated: false,
    ...overrides,
  }
}

function message(overrides: Partial<MessageRead>): MessageRead {
  return {
    id: 1,
    session_id: SESSION_ID,
    role: 'user',
    content: 'Read this starter project and explain how it is structured.',
    thinking: '',
    thinking_ms: 0,
    retrieval_trimmed: false,
    omitted_document_count: 0,
    tool_activity: [],
    created_at: '2026-09-02T00:00:00Z',
    agent_attempt: null,
    ...overrides,
  }
}
