import { act, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { StreamingMarkdown } from '@/components/chat/streaming-markdown'

function words(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>('[data-stream-word]'))
}

/** Set `document.visibilityState` for the duration of a test (jsdom owns it by default). */
function setVisibility(state: string) {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: state })
}

/**
 * A long-enough source that the reparse schedule holds rather than re-parsing on every
 * commit (above {@link STREAM_REPARSE_MIN_CHARS}), with a trailing sentinel word the tests
 * check for.
 */
const LONG_A = 'alpha '.repeat(300) + ' SENTINEL_A'
const LONG_B = 'beta '.repeat(300) + ' SENTINEL_B'

/** Forces `prefers-reduced-motion` on, which the default setup stub reports as off. */
function stubReducedMotion(reduce: boolean) {
  vi.stubGlobal(
    'matchMedia',
    vi.fn((query: string) => ({
      matches: reduce && query.includes('prefers-reduced-motion'),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    })),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('StreamingMarkdown', () => {
  describe('markdown rendering', () => {
    it('renders headings and prose', () => {
      const { container } = render(<StreamingMarkdown content={'# Title\n\nSome prose.'} />)
      expect(container.querySelector('h1')).toHaveTextContent('Title')
      expect(container).toHaveTextContent('Some prose.')
    })

    it('renders GFM tables inside a scroll container', () => {
      // Wide content must scroll inside its own surface rather than overflowing the page.
      const { container } = render(
        <StreamingMarkdown content={'| a | b |\n| - | - |\n| 1 | 2 |'} />,
      )
      const table = container.querySelector('table')
      expect(table).not.toBeNull()
      expect(table?.parentElement?.className).toContain('overflow-x-auto')
    })

    it('renders fenced code as a code block', () => {
      const { container } = render(<StreamingMarkdown content={'```js\nconst a = 1\n```'} />)
      expect(container.querySelector('pre code')).toHaveTextContent('const a = 1')
    })

    it('typesets display math with KaTeX', () => {
      const { container } = render(<StreamingMarkdown content={'$$x^2$$'} />)
      expect(container.querySelector('.katex-display, .katex')).not.toBeNull()
    })

    it('does not throw on malformed math', () => {
      // KaTeX runs with throwOnError off; a half-written equation must not blank the reply.
      expect(() => render(<StreamingMarkdown content={'$$\\frac{1}{$$'} />)).not.toThrow()
    })
  })

  describe('word splitting is streaming-only', () => {
    it('adds no reveal spans to a settled message', () => {
      const { container } = render(<StreamingMarkdown content="one two three" />)
      expect(words(container)).toHaveLength(0)
    })

    it('wraps each prose word while streaming', () => {
      const { container } = render(<StreamingMarkdown content="one two three" streaming />)
      expect(words(container)).toHaveLength(3)
    })

    it('never splits words inside a code block', () => {
      // Code is byte-for-byte intact; splitting it would corrupt what the reader copies.
      const { container } = render(
        <StreamingMarkdown content={'```js\nconst a = 1\n```'} streaming />,
      )
      const code = container.querySelector('pre')
      expect(code?.querySelectorAll('[data-stream-word]')).toHaveLength(0)
    })

    it('never splits inline code', () => {
      const { container } = render(<StreamingMarkdown content="call `foo bar` now" streaming />)
      const inline = container.querySelector('code')
      expect(inline?.querySelectorAll('[data-stream-word]')).toHaveLength(0)
      expect(inline).toHaveTextContent('foo bar')
    })

    it('reveals an equation as one piece rather than fragment by fragment', () => {
      const { container } = render(<StreamingMarkdown content={'$$\\frac{1}{2}$$'} streaming />)
      const revealed = words(container)
      expect(revealed).toHaveLength(1)
      expect(revealed[0].dataset.streamWord).toBe('equation-0')
    })

    it('reveals a list item with its marker, which no text node carries', () => {
      // The bullet is painted from `list-item`, so an untagged item stamps its marker down
      // complete and the words then fall into place around it.
      const { container } = render(<StreamingMarkdown content={'- one\n- two'} streaming />)
      const items = Array.from(container.querySelectorAll<HTMLElement>('li[data-stream-word]'))
      expect(items.map((item) => item.dataset.streamWord)).toEqual(['item-0', 'item-1'])
      // Still a marker, and still its own words: the item is a unit, not a replacement for
      // the split below it.
      expect(items[0].querySelectorAll('span[data-stream-word]')).toHaveLength(1)
    })

    it('reveals a numbered item with the first word on its line', () => {
      const { container } = render(<StreamingMarkdown content={'1. alpha beta'} streaming />)
      const units = words(container)
      const delayOf = (node: HTMLElement) =>
        Number.parseFloat(node.style.getPropertyValue('--stream-word-delay'))
      expect(units[0].tagName).toBe('LI')
      expect(delayOf(units[0])).toBe(delayOf(units[1]))
    })

    it('keys equations by order, not by source offset', () => {
      // A streamed answer only grows, so the third equation stays the third equation even as
      // its source offset moves while the fragment is closed and reopened.
      const { container } = render(
        <StreamingMarkdown content={'$$a$$\n\ntext\n\n$$b$$'} streaming />,
      )
      const keys = words(container)
        .map((node) => node.dataset.streamWord)
        .filter((key) => key?.startsWith('equation-'))
      expect(keys).toEqual(['equation-0', 'equation-1'])
    })
  })

  describe('reveal state across renders', () => {
    it('marks freshly arrived words visible', () => {
      const { container } = render(<StreamingMarkdown content="one two" streaming />)
      for (const node of words(container)) {
        expect(node.classList.contains('stream-word-visible')).toBe(true)
      }
    })

    it('keeps already-revealed words visible when more text arrives', () => {
      // Re-fading settled words on every token would make the whole reply shimmer.
      const { container, rerender } = render(<StreamingMarkdown content="one two" streaming />)
      rerender(<StreamingMarkdown content="one two three" streaming />)

      const revealed = words(container)
      expect(revealed).toHaveLength(3)
      for (const node of revealed) {
        expect(node.classList.contains('stream-word-visible')).toBe(true)
      }
    })

    it('keeps a pending unit in its slot when its node is replaced', () => {
      // Markdown is re-parsed on every frame, so React can replace the node holding a word
      // while its reveal is still pending. Re-applying the class without its delay would
      // jump it to the front of the queue, ahead of every word waiting in front of it.
      const { container, rerender } = render(
        <StreamingMarkdown content="one two three four five" streaming />,
      )
      const pending = words(container).at(-1)
      const scheduled = Number.parseFloat(pending!.style.getPropertyValue('--stream-word-delay'))
      pending!.classList.remove('stream-word-visible')
      pending!.style.removeProperty('--stream-word-delay')

      rerender(<StreamingMarkdown content="one two three four five six" streaming />)

      const restored = words(container)[4]
      expect(restored.classList.contains('stream-word-visible')).toBe(true)
      const delay = Number.parseFloat(restored.style.getPropertyValue('--stream-word-delay'))
      expect(delay).toBeGreaterThan(0)
      expect(delay).toBeLessThanOrEqual(scheduled)
    })

    it('gives a later word a non-zero delay so the cascade is ordered', () => {
      const { container } = render(
        <StreamingMarkdown content="one two three four five" streaming />,
      )
      const delays = words(container).map((node) =>
        Number.parseFloat(node.style.getPropertyValue('--stream-word-delay')),
      )
      expect(delays[0]).toBe(0)
      expect(delays.at(-1)).toBeGreaterThan(0)
      expect([...delays].sort((a, b) => a - b)).toEqual(delays)
    })
  })

  describe('reveal completion', () => {
    it('reports completion immediately for a settled message', () => {
      const onRevealComplete = vi.fn()
      render(<StreamingMarkdown content="done" onRevealComplete={onRevealComplete} />)
      expect(onRevealComplete).toHaveBeenCalled()
    })

    it('reports completion immediately for empty streaming content', () => {
      const onRevealComplete = vi.fn()
      render(<StreamingMarkdown content="" streaming onRevealComplete={onRevealComplete} />)
      expect(onRevealComplete).toHaveBeenCalled()
    })

    it('reports completion at once under reduced motion', () => {
      stubReducedMotion(true)
      const onRevealComplete = vi.fn()
      render(
        <StreamingMarkdown
          content="one two"
          streaming
          turnEnded
          onRevealComplete={onRevealComplete}
        />,
      )
      expect(onRevealComplete).toHaveBeenCalled()
    })

    it('reports completion after the cascade once the turn ends', async () => {
      stubReducedMotion(false)
      const onRevealComplete = vi.fn()
      const { rerender } = render(
        <StreamingMarkdown content="one two" streaming onRevealComplete={onRevealComplete} />,
      )
      onRevealComplete.mockClear()

      // The `done` frame arrives after the last token, so the turn can end with no content
      // change; completion still has to be reported.
      rerender(
        <StreamingMarkdown
          content="one two"
          streaming
          turnEnded
          onRevealComplete={onRevealComplete}
        />,
      )

      await waitFor(() => expect(onRevealComplete).toHaveBeenCalled())
    })
  })

  describe('long-answer reparse edges (PLA-511)', () => {
    it('re-parses a held long answer in full the moment the turn ends', () => {
      const { container, rerender } = render(
        <StreamingMarkdown content="seed" streaming />,
      )
      // Prime: a re-parse runs, stamping the schedule's last-parse time.
      rerender(<StreamingMarkdown content={LONG_A} streaming />)
      // A longer feed within the gap is held: the last document stays up, the tail is not
      // on screen yet.
      rerender(<StreamingMarkdown content={`${LONG_A} tail`} streaming />)
      expect(container.textContent).toContain('SENTINEL_A')
      expect(container.textContent).not.toContain('tail')
      // The terminal frame forces the full re-parse, synchronously: the held words land.
      rerender(<StreamingMarkdown content={`${LONG_A} tail`} streaming turnEnded />)
      expect(container.textContent).toContain('tail')
    })

    it('strands no held text across a generation reset, and drains the new generation', () => {
      const { container, rerender } = render(
        <StreamingMarkdown content="seed" streaming generation="gen1" />,
      )
      // Prime under gen1: a re-parse runs, stamping the schedule.
      rerender(<StreamingMarkdown content={LONG_A} streaming generation="gen1" />)
      // Held under the same generation: the longer feed does not drain yet.
      rerender(<StreamingMarkdown content={`${LONG_A} tail`} streaming generation="gen1" />)
      expect(container.textContent).toContain('SENTINEL_A')
      expect(container.textContent).not.toContain('tail')
      // A reset clears the answer and moves the generation in one commit: the old document's
      // held words must not strand, and must not drain under the new generation.
      rerender(<StreamingMarkdown content="" streaming generation="gen2" />)
      expect(container.textContent?.trim()).toBe('')
      // The new generation's words drain: a fresh document, no inheritance of the old slots.
      rerender(<StreamingMarkdown content={LONG_B} streaming generation="gen2" />)
      expect(container.textContent).toContain('SENTINEL_B')
    })

    it('holds a hidden long answer and flushes it whole on resume with no further tokens', async () => {
      setVisibility('hidden')
      try {
        // A short first answer parses immediately (even hidden) and marks the hidden parse.
        const { container, rerender } = render(
          <StreamingMarkdown content="seed" streaming />,
        )
        rerender(<StreamingMarkdown content="first words" streaming />)
        expect(container.textContent).toContain('first words')
        // A long feed while hidden is held: no frame is owed, the last document stays up.
        rerender(<StreamingMarkdown content={LONG_A} streaming />)
        expect(container.textContent).not.toContain('SENTINEL_A')
        // Resume with no further tokens: the held text must flush, not stay under the old
        // document. The return from hidden forces the full re-parse.
        setVisibility('visible')
        await act(async () => {
          document.dispatchEvent(new Event('visibilitychange'))
        })
        expect(container.textContent).toContain('SENTINEL_A')
      } finally {
        setVisibility('visible')
      }
    })
  })
})
