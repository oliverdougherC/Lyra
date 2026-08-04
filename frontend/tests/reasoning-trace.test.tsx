import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { ReasoningTrace } from '@/components/chat/reasoning-trace'

/**
 * Contracts from docs/ui-phase-1.md: the thought is always closed until the reader opens it,
 * live or settled, because it is the model's working rather than the reply. The header is a
 * trigger in both states, so a live thought is still reachable. A model that does not think
 * renders none of this.
 */
describe('ReasoningTrace', () => {
  it('renders nothing for a model that does not think', () => {
    const { container } = render(<ReasoningTrace text="" />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing for a thought that is only whitespace', () => {
    const { container } = render(<ReasoningTrace text={'   \n  '} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('starts closed when settled, so the answer is not pushed down the page', () => {
    render(<ReasoningTrace text="working through it" durationMs={12000} />)
    expect(screen.queryByText('working through it')).not.toBeInTheDocument()
  })

  it('starts closed while streaming', () => {
    render(<ReasoningTrace text="half a thought" streaming startedAt={Date.now()} />)
    expect(screen.queryByText('half a thought')).not.toBeInTheDocument()
  })

  it('reports the duration once thinking has stopped', () => {
    render(<ReasoningTrace text="done" durationMs={12000} />)
    expect(screen.getByRole('button')).toHaveTextContent('Thought for 12 seconds')
  })

  it('says Thinking when no duration was recorded', () => {
    // A message loaded from history before durations were stored carries no elapsed time.
    render(<ReasoningTrace text="done" />)
    expect(screen.getByRole('button')).toHaveTextContent('Thinking')
  })

  it('exposes a trigger while streaming, so a live thought is reachable', async () => {
    render(<ReasoningTrace text="live thought" streaming startedAt={Date.now()} />)
    const trigger = screen.getByRole('button')

    await userEvent.click(trigger)

    expect(screen.getByText('live thought')).toBeVisible()
  })

  it('opens and closes a settled thought', async () => {
    render(<ReasoningTrace text="settled thought" durationMs={3000} />)
    const trigger = screen.getByRole('button')

    await userEvent.click(trigger)
    expect(screen.getByText('settled thought')).toBeVisible()

    await userEvent.click(trigger)
    expect(screen.queryByText('settled thought')).not.toBeInTheDocument()
  })

  it('is operable from the keyboard', async () => {
    render(<ReasoningTrace text="keyboard thought" durationMs={1000} />)

    await userEvent.tab()
    expect(screen.getByRole('button')).toHaveFocus()

    await userEvent.keyboard('{Enter}')
    expect(screen.getByText('keyboard thought')).toBeVisible()
  })

  describe('duration wording', () => {
    it.each([
      [1000, 'Thought for 1 second'],
      [2000, 'Thought for 2 seconds'],
      [59000, 'Thought for 59 seconds'],
      [60000, 'Thought for 1 minute'],
      [120000, 'Thought for 2 minutes'],
      [90000, 'Thought for 1m 30s'],
    ])('renders %ims as "%s"', (ms, expected) => {
      render(<ReasoningTrace text="x" durationMs={ms} />)
      expect(screen.getByRole('button')).toHaveTextContent(expected)
    })
  })

  describe('markdown is deferred until the thought settles', () => {
    it('keeps a streaming thought as plain text', async () => {
      // Re-parsing thousands of characters per delta buys nothing on text moving faster
      // than anyone reads.
      const { container } = render(
        <ReasoningTrace text="# not a heading yet" streaming startedAt={Date.now()} />,
      )
      await userEvent.click(screen.getByRole('button'))

      expect(container.querySelector('h1')).toBeNull()
      expect(screen.getByText('# not a heading yet')).toBeVisible()
    })

    it('renders a settled thought as markdown', async () => {
      const { container } = render(<ReasoningTrace text="# a heading" durationMs={1000} />)
      await userEvent.click(screen.getByRole('button'))

      expect(container.querySelector('h1')).not.toBeNull()
    })
  })
})
