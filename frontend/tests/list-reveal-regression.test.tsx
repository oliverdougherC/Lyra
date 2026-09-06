import { render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { StreamingMarkdown } from '@/components/chat/streaming-markdown'
afterEach(() => vi.restoreAllMocks())
it('keeps list text on its original reveal schedule as the response grows', () => {
  let now = 1000
  vi.spyOn(performance, 'now').mockImplementation(() => now)
  const { container, rerender } = render(
    <StreamingMarkdown content={'Intro\n\n- first bullet'} streaming />,
  )
  const textNode = () =>
    Array.from(container.querySelectorAll<HTMLElement>('li span[data-stream-word]')).find(
      (n) => n.textContent === 'first',
    )!
  const initial = textNode()
  const initialKey = initial.dataset.streamWord
  const initialTime = now + parseFloat(initial.style.getPropertyValue('--stream-word-delay'))
  const chunks = [
    'Intro\n\n- first bullet\n- second bullet',
    'Intro\n\n- first bullet\n- second bullet\n\nEnd paragraph',
    'Intro\n\n- first bullet\n- second bullet\n\nEnd paragraph keeps streaming.',
  ]
  for (const content of chunks) {
    now += 300
    rerender(<StreamingMarkdown content={content} streaming />)
    const node = textNode()
    expect(node).toBe(initial)
    expect(node.dataset.streamWord).toBe(initialKey)
    expect(node.className).toContain('stream-word-visible')
    expect(Number(node.dataset.streamRevealAt)).toBeLessThanOrEqual(initialTime)
    expect(parseFloat(node.style.getPropertyValue('--stream-word-delay'))).toBe(initialTime - 1000)
  }
})

it('keeps fast streamed lists within half a second of the received text', () => {
  let now = 1000
  vi.spyOn(performance, 'now').mockImplementation(() => now)
  const { container, rerender } = render(<StreamingMarkdown content="" streaming />)
  let content = ''
  for (let i = 0; i < 35; i++) {
    content += `\n- item ${i} has several words`
    now += 40
    rerender(<StreamingMarkdown content={content} streaming />)
  }
  const tail = Array.from(container.querySelectorAll<HTMLElement>('[data-stream-word]')).at(-1)!
  expect(parseFloat(tail.style.getPropertyValue('--stream-word-delay'))).toBeLessThanOrEqual(500)
  expect(Number(tail.dataset.streamRevealAt) - now).toBeLessThanOrEqual(500)
  expect(Number(tail.dataset.streamRevealAt)).toBeGreaterThanOrEqual(now)
})

it('does not hide previously revealed list words when tight lists become loose lists', () => {
  let now = 1000
  vi.spyOn(performance, 'now').mockImplementation(() => now)
  const { container, rerender } = render(
    <StreamingMarkdown content={'- first\n- second'} streaming />,
  )
  const original = container.querySelector('li span[data-stream-word]')
  now += 800
  rerender(<StreamingMarkdown content={'- first\n\n- second'} streaming />)
  const replaced = container.querySelector<HTMLElement>('li span[data-stream-word]')!
  expect(replaced).not.toBe(original)
  expect(replaced.textContent).toBe('first')
  expect(parseFloat(replaced.style.getPropertyValue('--stream-word-delay'))).toBeLessThan(-180)
})
