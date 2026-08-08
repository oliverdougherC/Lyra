import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { AgentActivityFeed } from '@/components/agent'

describe('AgentActivityFeed', () => {
  it('renders durable source and proposal references beside tool outcomes', () => {
    render(
      <AgentActivityFeed
        entries={[
          {
            id: 'evt-1',
            title: 'Searched the public web',
            toolLabel: 'search_web',
            summary: 'Looked for the lab write-up method name.',
            targetLabel: 'query: finite difference stencil',
            status: 'completed',
            truncated: true,
            source: {
              id: 14,
              kind: 'web',
              title: 'Finite Difference Methods',
              detail: 'Snapshotted after fetch.',
              href: 'https://example.com',
              truncated: true,
            },
            proposal: {
              id: 8,
              kind: 'profile_fact',
              title: 'Suggest adding a method fact',
              state: 'pending',
            },
          },
        ]}
      />,
    )

    expect(screen.getByText('Searched the public web')).toBeInTheDocument()
    expect(screen.getByText('Completed')).toBeInTheDocument()
    expect(screen.getAllByText(/Truncated|Excerpt truncated/)).not.toHaveLength(0)
    expect(screen.getByLabelText('Source Finite Difference Methods')).toBeInTheDocument()
    expect(screen.getByLabelText('Proposal Suggest adding a method fact')).toBeInTheDocument()
  })

  it('shows disabled and unavailable states without pretending work happened', () => {
    render(
      <AgentActivityFeed
        entries={[
          {
            id: 'evt-2',
            title: 'Read workspace file',
            toolLabel: 'read_workspace_file',
            status: 'disabled',
            disabledReason: 'Workspace read is off for this class.',
          },
          {
            id: 'evt-3',
            title: 'Confirm command',
            toolLabel: 'propose_verification_command',
            status: 'unavailable',
            disabledReason: 'No workspace is attached.',
          },
        ]}
      />,
    )

    expect(screen.getByText('Disabled by policy')).toBeInTheDocument()
    expect(screen.getByText('Temporarily unavailable')).toBeInTheDocument()
    expect(screen.getByText('Workspace read is off for this class.')).toBeInTheDocument()
    expect(screen.getByText('No workspace is attached.')).toBeInTheDocument()
  })
})
