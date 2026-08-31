import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { CapabilitySummary, type AgentGrantState } from '@/components/agent'

const GRANTS: AgentGrantState[] = [
  {
    key: 'web',
    label: 'Web',
    description: 'Allow public web search and fetch through Exa.',
    enabled: true,
    inherited: true,
  },
  {
    key: 'workspace_read',
    label: 'Workspace read',
    description: 'Allow read-only inspection of the attached repository.',
    enabled: false,
    blockedReason: 'Attach a workspace before enabling workspace read.',
  },
  {
    key: 'change_proposals',
    label: 'Change proposals',
    description: 'Allow inert file-change proposals for reviewed hunks.',
    enabled: false,
    blockedReason: 'Workspace read must be enabled first.',
    pendingInvalidationCount: 2,
  },
  {
    key: 'commands',
    label: 'Commands',
    description: 'Allow exact verification command proposals.',
    enabled: false,
    unavailable: true,
    note: 'Command confirmation stays per-run even when this grant is on.',
  },
]

describe('CapabilitySummary', () => {
  it('keeps workspace attachment separate from grants', () => {
    render(
      <CapabilitySummary
        workspace={{ label: 'signals-lab', rootPath: '/Users/me/signals' }}
        grants={GRANTS}
      />,
    )

    expect(screen.getByText('Attached workspace')).toBeInTheDocument()
    expect(screen.getByText('signals-lab')).toBeInTheDocument()
    expect(
      screen.getByText('Attaching a workspace enables none of the grants below.'),
    ).toBeInTheDocument()
    expect(screen.getByText('Inherited on')).toBeInTheDocument()
    expect(screen.getByText('Unavailable')).toBeInTheDocument()
  })

  it('renders the unattached empty state honestly', () => {
    render(<CapabilitySummary workspace={null} grants={GRANTS} />)

    expect(screen.getByText('No workspace attached')).toBeInTheDocument()
    expect(screen.getByText(/remain unavailable until a root is attached/i)).toBeInTheDocument()
  })

  it('emits grant toggles with the next enabled state', async () => {
    const onToggleGrant = vi.fn()
    render(
      <CapabilitySummary
        workspace={{ label: 'signals-lab', rootPath: '/Users/me/signals' }}
        grants={[
          {
            key: 'web',
            label: 'Web',
            description: 'Allow public web search and fetch through Exa.',
            enabled: false,
          },
        ]}
        onToggleGrant={onToggleGrant}
      />,
    )

    await userEvent.click(screen.getByRole('button', { name: 'Enable Web' }))

    expect(onToggleGrant).toHaveBeenCalledWith('web', true)
  })
})
