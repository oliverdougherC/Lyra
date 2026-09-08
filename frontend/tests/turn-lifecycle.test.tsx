import { act, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { StreamingMarkdown } from '@/components/chat/streaming-markdown'
import { revealWork } from '@/components/chat/reveal'

function units(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>('[data-stream-word]'))
}

function revealAt(node: HTMLElement): number {
  return Number(node.dataset.streamRevealAt)
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('answer generations (PLA-501)', () => {
  it('keeps the old schedule across a content change that does not change the generation', () => {
    // A re-parse is not a new answer: the words that survive it keep their slots, and a
    // word re-formed inside a range the reader holds inherits that range's moment. This
    // is why the reset case must come with a generation change rather than one.
    vi.spyOn(performance, 'now').mockReturnValue(1000)
    const { container, rerender } = render(<StreamingMarkdown content="The old answer" streaming />)
    const first = units(container)[0]
    expect(revealAt(first)).toBe(1000)

    vi.spyOn(performance, 'now').mockReturnValue(5000)
    rerender(<StreamingMarkdown content="The old answer extended" streaming />)
    const kept = units(container)[0]
    expect(kept.dataset.streamWord).toBe(first.dataset.streamWord)
    expect(revealAt(kept)).toBe(1000)
  })

  it('clears the old schedule when the generation changes in the same batch as the replacement', () => {
    // A `reset` and the first token of its replacement can land in one network read and one
    // React batch: there is no empty render between them, so "the text went away" never
    // happens. The only evidence of a new answer is the generation.
    vi.spyOn(performance, 'now').mockReturnValue(1000)
    const { container, rerender } = render(
      <StreamingMarkdown content="The old answer" streaming generation="g1" />,
    )
    // Advance time so the old answer's words carry past deadlines under `stream-0`.
    vi.spyOn(performance, 'now').mockReturnValue(5000)

    act(() => {
      rerender(<StreamingMarkdown content="Revised answer" streaming generation="g2" />)
    })
    // 'Revised' anchors at offset 0 — the same key the old answer's first word held.
    const revised = units(container)[0]
    expect(revised.dataset.streamWord).toBe('stream-0')
    // Without the generation change it would start life at the old word's moment (1000),
    // appearing already revealed. The reset's replacement starts at now.
    expect(revealAt(revised)).toBeGreaterThanOrEqual(5000)

    // The second word takes a fresh slot after the first, not a slot the old answer owned.
    expect(revealAt(units(container)[1])).toBeGreaterThan(revealAt(revised))
  })

  it('reports each drain with the generation it belongs to', async () => {
    // A drain only matters once the turn has ended, so `turnEnded` is what arms the report.
    vi.spyOn(performance, 'now').mockReturnValue(1000)
    const drains: (string | undefined)[] = []
    const { rerender } = render(
      <StreamingMarkdown
        content="one two"
        streaming
        turnEnded
        generation="g1"
        onRevealComplete={(generation) => drains.push(generation)}
      />,
    )
    await vi.waitFor(() => expect(drains).toHaveLength(1))
    expect(drains[0]).toBe('g1')

    // A replacement generation that ends with the same content drains for itself, not for
    // the generation it replaced.
    drains.length = 0
    rerender(
      <StreamingMarkdown
        content="one two"
        streaming
        turnEnded
        generation="g2"
        onRevealComplete={(generation) => drains.push(generation)}
      />,
    )
    await vi.waitFor(() => expect(drains).toHaveLength(1))
    expect(drains[0]).toBe('g2')
  })

  it('does not rewrite the finished units of a long answer when a new tail arrives', () => {
    // A commit must not re-write the style of every unit already on screen: on a long
    // settled answer the only new work is the new tail.
    vi.spyOn(performance, 'now').mockReturnValue(1000)
    const source = Array.from({ length: 20 }, (_, index) => `word${index}`).join(' ')
    const { container, rerender } = render(<StreamingMarkdown content={source} streaming />)
    expect(units(container).length).toBeGreaterThan(10)

    // Let the whole cascade finish, then append one word.
    vi.spyOn(performance, 'now').mockReturnValue(60000)
    rerender(<StreamingMarkdown content={`${source} last`} streaming />)

    // The finished units keep their fixed delays — the only write this commit makes is the
    // new word's.
    expect(revealWork.styleWrites).toBe(1)
    expect(units(container).length).toBeGreaterThanOrEqual(21)
  })
})
