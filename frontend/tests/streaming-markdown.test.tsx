import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { StreamingMarkdown } from '@/components/chat/streaming-markdown'

function words(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>('[data-stream-word]'))
}

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
})
