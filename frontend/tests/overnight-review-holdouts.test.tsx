import { render } from '@testing-library/react'
import { expect, it, vi, afterEach } from 'vitest'
import { normalizeMarkdownForRender } from '@/components/chat/markdown-utils'
import { StreamingMarkdown } from '@/components/chat/streaming-markdown'

afterEach(() => vi.restoreAllMocks())

it('preserves an explicitly inline fraction when a newline arrives', () => {
  const source = '- First $\\frac{1}{2}$'
  expect(normalizeMarkdownForRender(source, true)).not.toContain('$$')
  expect(normalizeMarkdownForRender(source + '\n', true)).not.toContain('$$')
})

it('preserves an explicitly inline fraction at completion without trailing newline', () => {
  const source = '- First $\\frac{1}{2}$'
  expect(normalizeMarkdownForRender(source)).toBe(normalizeMarkdownForRender(source, true))
})

it('keeps already visible source characters visible when emphasis splits a word', () => {
  let now = 1000
  vi.spyOn(performance, 'now').mockImplementation(() => now)
  const { container, rerender } = render(<StreamingMarkdown content={'a**b*c $x$'} streaming />)
  now = 1800
  rerender(<StreamingMarkdown content={'a**b*c $x$***'} streaming />)
  for (const node of container.querySelectorAll<HTMLElement>('span[data-stream-word]')) {
    if (node.textContent === 'b' || node.textContent === 'c') {
      expect(Number(node.dataset.streamRevealAt), node.textContent).toBeLessThanOrEqual(1620)
    }
  }
})

it('renders two prices as prose instead of a fake math equation', () => {
  const { container } = render(
    <StreamingMarkdown content={'It costs $5 and $10 today.'} streaming />,
  )
  expect(container.querySelector('.katex')).toBeNull()
  expect(container.textContent).toContain('$5 and $10')
})
