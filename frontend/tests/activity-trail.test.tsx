import { render, screen } from '@testing-library/react'
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
  it('renders a stored trail with its outcomes, failures included', () => {
    renderRow(<MessageRow message={message({ tool_activity: TRAIL })} />)

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

  it('renders no trail at all on an ordinary tutor message', () => {
    renderRow(<MessageRow message={message({})} />)

    expect(screen.queryByLabelText('What Lyra did for this reply')).not.toBeInTheDocument()
  })
})
