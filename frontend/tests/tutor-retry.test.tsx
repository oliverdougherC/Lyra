/**
 * Tutor-turn failure presentation and retry UX (PLA-306 Finding 5).
 *
 * The transcript must show a truthful failure state for failed and stopped
 * tutor turns, offer Retry only on the last user message that failed or
 * stopped, and never show failure UI for completed turns.
 */
import { cleanup, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { MessageRow, type ChatMessage } from '@/components/chat/message-bubble'
import { TooltipProvider } from '@/components/ui/tooltip'
import type { TutorAttempt } from '@/types'

function renderRow(msg: ChatMessage, overrides: Partial<Parameters<typeof MessageRow>[0]> = {}) {
  return render(
    <TooltipProvider>
      <MessageRow message={msg} {...overrides} />
    </TooltipProvider>,
  )
}

function message(overrides: Partial<ChatMessage>): ChatMessage {
  return {
    id: 1,
    role: 'user',
    content: 'What is the derivative of x^2?',
    thinking: '',
    thinking_ms: 0,
    retrieval_trimmed: false,
    omitted_document_count: 0,
    tool_activity: [],
    created_at: '2026-08-26T00:00:00Z',
    ...overrides,
  }
}

function tutorAttempt(
  state: TutorAttempt['state'],
  detail: string | null = null,
): TutorAttempt {
  return { state, stopped_reason: state === 'completed' ? null : state, detail }
}

describe('failed tutor turn presentation (PLA-306)', () => {
  it('renders a failure notice under the question with the bounded detail', () => {
    renderRow(
      message({
        tutor_attempt: tutorAttempt('failed', 'The tutor endpoint could not be reached.'),
      }),
    )
    const failure = document.querySelector('[data-tutor-turn-failure]')
    expect(failure).not.toBeNull()
    expect(failure).toHaveTextContent('The tutor endpoint could not be reached.')
  })

  it('renders a stopped turn as a failure too', () => {
    renderRow(
      message({
        tutor_attempt: tutorAttempt('stopped', 'This turn was interrupted before it finished.'),
      }),
    )
    const failure = document.querySelector('[data-tutor-turn-failure]')
    expect(failure).not.toBeNull()
    expect(failure).toHaveTextContent('This turn was interrupted before it finished.')
  })

  it('uses the default message when detail is null', () => {
    renderRow(message({ tutor_attempt: tutorAttempt('failed') }))
    const failure = document.querySelector('[data-tutor-turn-failure]')
    expect(failure).not.toBeNull()
    expect(failure).toHaveTextContent('This turn did not finish.')
  })

  it('shows nothing extra for a completed turn', () => {
    renderRow(message({ tutor_attempt: tutorAttempt('completed') }))
    expect(document.querySelector('[data-tutor-turn-failure]')).toBeNull()
  })

  it('shows nothing extra when there is no tutor attempt', () => {
    renderRow(message({ tutor_attempt: null }))
    expect(document.querySelector('[data-tutor-turn-failure]')).toBeNull()
    cleanup()
    renderRow(message({}))
    expect(document.querySelector('[data-tutor-turn-failure]')).toBeNull()
  })
})

describe('retry button visibility (PLA-306)', () => {
  it('shows a retry button on a failed tutor turn when onRetry is provided', () => {
    const retryFn = () => {}
    renderRow(message({ tutor_attempt: tutorAttempt('failed') }), { onRetry: retryFn })
    const button = screen.getByRole('button', { name: /try again/i })
    expect(button).toBeInTheDocument()
  })

  it('shows a retry button on a stopped tutor turn when onRetry is provided', () => {
    const retryFn = () => {}
    renderRow(message({ tutor_attempt: tutorAttempt('stopped') }), { onRetry: retryFn })
    const button = screen.getByRole('button', { name: /try again/i })
    expect(button).toBeInTheDocument()
  })

  it('does not show a retry button when onRetry is not provided', () => {
    renderRow(message({ tutor_attempt: tutorAttempt('failed') }))
    expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()
  })

  it('does not show failure UI for a completed turn even with onRetry', () => {
    renderRow(message({ tutor_attempt: tutorAttempt('completed') }), { onRetry: () => {} })
    expect(document.querySelector('[data-tutor-turn-failure]')).toBeNull()
  })
})

describe('tutor failure does not bleed into other attempt types (PLA-306)', () => {
  it('does not show tutor failure when only agent_attempt is failed', () => {
    renderRow(
      message({
        agent_attempt: { state: 'failed', stopped_reason: 'upstream_failed', detail: 'agent err' },
        tutor_attempt: null,
      }),
    )
    expect(document.querySelector('[data-tutor-turn-failure]')).toBeNull()
    expect(document.querySelector('[data-agent-turn-failure]')).not.toBeNull()
  })

  it('does not show agent failure when only tutor_attempt is failed', () => {
    renderRow(
      message({
        tutor_attempt: tutorAttempt('failed', 'tutor err'),
        agent_attempt: null,
      }),
    )
    expect(document.querySelector('[data-agent-turn-failure]')).toBeNull()
    expect(document.querySelector('[data-tutor-turn-failure]')).not.toBeNull()
  })
})
