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

  it('drains an empty replacement under the new generation, not the one it replaced', async () => {
    // A reset whose replacement is empty (a cleared answer that ends immediately) must
    // report its drain under the new generation. The pane rejects a drain that names a
    // generation it no longer owns; an old-generation drain is the difference between a
    // turn that settles and one that is stuck forever.
    vi.spyOn(performance, 'now').mockReturnValue(1000)
    const drains: (string | undefined)[] = []
    const { rerender, unmount } = render(
      <StreamingMarkdown
        content="one two"
        streaming
        turnEnded
        generation="g1"
        onRevealComplete={(generation) => drains.push(generation)}
      />,
    )
    await vi.waitFor(() => expect(drains).toEqual(['g1']))

    // The replacement arrives already settled: empty content, a new generation. There is
    // no queue left to wait out, and the drain is owed now, under g2.
    drains.length = 0
    vi.spyOn(performance, 'now').mockReturnValue(5000)
    rerender(
      <StreamingMarkdown
        content=""
        streaming
        turnEnded
        generation="g2"
        onRevealComplete={(generation) => drains.push(generation)}
      />,
    )
    expect(drains).toEqual(['g2'])
    unmount()

    // A whitespace-only replacement is the same answer as an empty one: no units to
    // schedule, a drain under the new generation once the wait runs out.
    const whitespace: (string | undefined)[] = []
    const again = render(
      <StreamingMarkdown
        content="one two"
        streaming
        generation="g1"
        onRevealComplete={(generation) => whitespace.push(generation)}
      />,
    )
    act(() => {
      vi.spyOn(performance, 'now').mockReturnValue(8000)
      again.rerender(
        <StreamingMarkdown
          content="   "
          streaming
          turnEnded
          generation="g2"
          onRevealComplete={(generation) => whitespace.push(generation)}
        />,
      )
    })
    await vi.waitFor(() => expect(whitespace).toEqual(['g2']))
    again.unmount()
  })

  it('re-arms the settle wait when the settled answer content changes', async () => {
    // The accepted final content can replace the streamed text after the cascade drained:
    // a terminal result arrives with a longer body, same generation, turn already ended.
    // The settled wait must run out of the NEW tail — the tail that already drained
    // cannot drain again, and a wait armed against it would settle before the new words
    // are on screen.
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
    await vi.waitFor(() => expect(drains).toEqual(['g1']))

    drains.length = 0
    vi.spyOn(performance, 'now').mockReturnValue(5000)
    rerender(
      <StreamingMarkdown
        content="one two three four"
        streaming
        turnEnded
        generation="g1"
        onRevealComplete={(generation) => drains.push(generation)}
      />,
    )
    // The tail's slots were just laid out: the report that was already owed for the old
    // tail cannot be the only one the caller ever sees.
    await vi.waitFor(() => expect(drains).toEqual(['g1']))
  })

  it('never reports a drain from a generation that no longer owns the queue', async () => {
    // A turn ends and the settle wait arms against the g1 queue. Before that clock runs
    // out the answer is replaced by a new generation: the arm-time identity the old
    // timer carried is stale, and the re-arm cancels it. Whatever the old clock would
    // have reported, it is not the replacement's drain.
    vi.spyOn(performance, 'now').mockReturnValue(1000)
    const drains: (string | undefined)[] = []
    const { rerender } = render(
      <StreamingMarkdown
        content="one two"
        streaming
        generation="g1"
        onRevealComplete={(generation) => drains.push(generation)}
      />,
    )
    vi.spyOn(performance, 'now').mockReturnValue(2000)
    rerender(
      <StreamingMarkdown
        content="one two"
        streaming
        turnEnded
        generation="g1"
        onRevealComplete={(generation) => drains.push(generation)}
      />,
    )

    // The replacement lands before the g1 wait can fire: new generation, settled at once.
    vi.spyOn(performance, 'now').mockReturnValue(3000)
    rerender(
      <StreamingMarkdown
        content=""
        streaming
        turnEnded
        generation="g2"
        onRevealComplete={(generation) => drains.push(generation)}
      />,
    )
    expect(drains).toEqual(['g2'])

    // Run every clock out: the cancelled g1 arm reports nothing at all, and the re-armed
    // wait for the empty queue reports only its own generation.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 400))
    })
    expect(drains.length).toBeGreaterThanOrEqual(1)
    expect(drains.every((generation) => generation === 'g2')).toBe(true)

    // And a rapid next turn, a third generation, adds no report of its own until IT ends.
    rerender(
      <StreamingMarkdown
        content="fresh answer"
        streaming
        generation="g3"
        onRevealComplete={(generation) => drains.push(generation)}
      />,
    )
    expect(drains.every((generation) => generation === 'g2')).toBe(true)
  })
})
