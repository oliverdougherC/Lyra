import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { describe, expect, it } from 'vitest'

import { MessageRow, type ChatMessage } from '@/components/chat/message-bubble'
import { TooltipProvider } from '@/components/ui/tooltip'
import type { AgentChatActivity, WriterActivity } from '@/types'

function renderRow(node: ReactNode) {
  // MessageActions carries tooltips, and tooltips need their provider.
  return render(<TooltipProvider>{node}</TooltipProvider>)
}

function message(overrides: Partial<ChatMessage>): ChatMessage {
  return {
    id: 2,
    role: 'assistant',
    content: 'Your intro carries the argument.',
    thinking: '',
    thinking_ms: 0,
    retrieval_trimmed: false,
    omitted_document_count: 0,
    tool_activity: [],
    created_at: '2026-08-06 09:00:00',
    ...overrides,
  }
}

const TRAIL: WriterActivity[] = [
  { tool: 'read_section', label: 'Reading section "Introduction"', ok: true },
  {
    tool: 'search_course_material',
    label: 'Searching the course material for "entropy"',
    ok: false,
  },
]

describe('the activity trail on a message', () => {
  it('keeps the source-specific agent trail in expandable reply details', async () => {
    const activity: AgentChatActivity[] = [
      {
        audit_id: 'one',
        tool: 'read_workspace_file',
        capability: 'workspace_read',
        effect: 'pure',
        state: 'succeeded',
        target_kind: 'file',
        target_id: 'notes/chapter.md',
      },
      {
        audit_id: 'two',
        tool: 'search_web',
        capability: 'web',
        effect: 'network',
        state: 'refused',
        target_kind: null,
        target_id: null,
      },
      {
        audit_id: 'three',
        tool: 'read_document_page',
        capability: 'document_read',
        effect: 'database_read',
        state: 'succeeded',
        target_kind: 'document',
        target_id: '7',
        class_id: 1,
        sources: [
          {
            document_id: 7,
            filename: 'signals.pdf',
            page_number: 5,
            text_start: 0,
            text_end: 2600,
            text_length: 7500,
          },
        ],
        detail: 'More source text available',
        has_more: true,
      },
    ]
    renderRow(<MessageRow message={message({ tool_activity: activity })} agent />)
    expect(screen.queryByLabelText('What Lyra did for this reply')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Details' }))
    const trail = screen.getByLabelText('What Lyra did for this reply')
    expect(trail).toBeVisible()
    expect(trail).toHaveTextContent('read workspace file · notes/chapter.md')
    expect(trail).toHaveTextContent('search web')
    expect(trail).toHaveTextContent('Refused')
    expect(trail).toHaveTextContent('signals.pdf p. 5 chars 1–2600')
    expect(trail).toHaveTextContent('More source text available')
    expect(screen.getByRole('link', { name: 'signals.pdf p. 5' })).toHaveAttribute(
      'href',
      '/#/classes/1?tab=files&lyra-anchor=document-7&source-page=5',
    )
  })

  it('keeps a settled trail collapsed by default, behind one Details disclosure', async () => {
    renderRow(<MessageRow message={message({ tool_activity: TRAIL })} />)

    // The answer is what the reader came for: the trail does not sit above it expanded.
    expect(screen.queryByLabelText('What Lyra did for this reply')).not.toBeInTheDocument()
    const details = screen.getByRole('button', { name: 'Details' })

    await userEvent.click(details)

    const trail = screen.getByLabelText('What Lyra did for this reply')
    expect(trail).toHaveTextContent('Reading section "Introduction"')
    // A failed call stays in the record rather than being smoothed over.
    expect(trail).toHaveTextContent('Searching the course material for "entropy"')
  })

  it('prefers the live trail while streaming, so frames land as they arrive', () => {
    renderRow(
      <MessageRow
        message={message({ content: '', tool_activity: [] })}
        streaming
        activity={[TRAIL[0]]}
      />,
    )

    expect(screen.getByText('Reading section "Introduction"')).toBeInTheDocument()
  })

  it('summarizes an observed tool alongside expandable reasoning, without leaking its text', async () => {
    renderRow(
      <MessageRow
        message={message({ content: '', thinking: 'Secret intermediate answer' })}
        streaming
        activity={[TRAIL[0]]}
      />,
    )
    const trigger = screen.getByRole('button', { name: /Read a section/ })
    expect(screen.queryByText('Secret intermediate answer')).not.toBeInTheDocument()
    await userEvent.click(trigger)
    expect(screen.getByText('Secret intermediate answer')).toBeVisible()
  })

  it('clears live activity as soon as the turn ends, while keeping details', () => {
    const { container } = renderRow(
      <MessageRow
        message={message({ content: '', thinking: 'A thought' })}
        streaming
        turnEnded
        activity={[TRAIL[0]]}
      />,
    )
    expect(container.querySelector('[aria-busy="true"]')).toBeNull()
    expect(screen.getByRole('button', { name: 'Thought' })).toBeInTheDocument()
    expect(screen.getByText(TRAIL[0].label)).toBeInTheDocument()
  })

  it('shows a concise activity status without reasoning and keeps failed outcomes honest', () => {
    const { container } = renderRow(
      <MessageRow message={message({ content: '' })} streaming activity={TRAIL} />,
    )
    expect(screen.getByText('Thinking')).toBeInTheDocument()
    expect(container.querySelector('.animate-pulse')).toBeNull()
  })

  it('renders no trail at all on an ordinary tutor message', () => {
    renderRow(<MessageRow message={message({})} />)

    expect(screen.queryByLabelText('What Lyra did for this reply')).not.toBeInTheDocument()
  })
})
