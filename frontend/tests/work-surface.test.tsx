import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { AgentWorkSurface } from '@/components/agent/work-surface'
import { api } from '@/lib/api'
import * as runtime from '@/lib/runtime'
import type { AgentAuditEventRead } from '@/types'

const CLASS_ID = 9
const SESSION_ID = 11

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
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

  it('offers attach through the folder picker, with a bounded path entry when no picker exists', async () => {
    // No workspace attached: the only actionable request is attach.
    const attach = vi.spyOn(api, 'attachAgentWorkspace').mockResolvedValue(workspace())
    const { wrapper } = createWrapper()

    render(<AgentWorkSurface classId={CLASS_ID} sessionId={SESSION_ID} />, { wrapper })

    // The workspace is absent, so the compact attach affordance is the entry point - not a
    // grant card, and not a setup section.
    fireEvent.click(screen.getByRole('button', { name: /attach a folder/i }))
    // No native picker in the test environment: the bounded path entry appears.
    const input = await screen.findByLabelText('Attach a local folder')
    fireEvent.change(input, { target: { value: '/tmp/starter' } })
    fireEvent.click(screen.getByRole('button', { name: 'Attach' }))
    await waitFor(() =>
      expect(attach).toHaveBeenCalledWith(CLASS_ID, '/tmp/starter', {
        displayName: undefined,
        readEnabled: true,
      }),
    )
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
    expect(api.updateAgentWorkspaceGrants).not.toHaveBeenCalled()
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
    expect(screen.getByText(/Lyra can list and read the text files in the folder\./)).toBeInTheDocument()
    expect(screen.getByText(/each still need their own approval/)).toBeInTheDocument()
  })
})
