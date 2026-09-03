import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { AgentWorkSurface } from '@/components/agent/work-surface'
import { WorkspaceAttachProvider, WorkspaceContextChip } from '@/components/agent/workspace-attach'
import { api } from '@/lib/api'
import * as runtime from '@/lib/runtime'
import type { AgentAuditEventRead, MessageRead } from '@/types'

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

    fireEvent.click(await screen.findByRole('button', { name: 'Attach folder' }))
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
    await screen.findByRole('button', { name: 'Attach folder' })
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
    fireEvent.click(screen.getByRole('button', { name: 'Attach a folder' }))
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
})

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
