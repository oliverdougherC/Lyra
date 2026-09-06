import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { describe, expect, it } from 'vitest'

import { MessageRow, type ChatMessage } from '@/components/chat/message-bubble'
import { TooltipProvider } from '@/components/ui/tooltip'
import type { WriterActivity } from '@/types'

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
