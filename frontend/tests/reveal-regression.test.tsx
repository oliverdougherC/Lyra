import { render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { StreamingMarkdown } from '@/components/chat/streaming-markdown'

afterEach(() => vi.restoreAllMocks())

/** The units of the rendered answer, in document reading order. */
function units(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>('[data-stream-word]'))
}

/** The scheduled reveal moment a unit was given, in wall-clock terms. */
function revealAt(node: HTMLElement): number {
  return Number(node.dataset.streamRevealAt)
}

/** Reading order must never break: deadlines are nondecreasing down the document. */
function expectOrdered(container: HTMLElement): void {
  const times = units(container).map(revealAt)
  for (let i = 1; i < times.length; i += 1) {
    expect(times[i], `unit ${i} reveals before unit ${i - 1}`).toBeGreaterThanOrEqual(times[i - 1])
  }
}

it('incomplete emphasis that completes keeps its words and their slots', () => {
  let now = 1000
  vi.spyOn(performance, 'now').mockImplementation(() => now)
  const { container, rerender } = render(<StreamingMarkdown content={'**alpha beta'} streaming />)
  const firstPass = units(container).map((node) => ({
    key: node.dataset.streamWord,
    at: revealAt(node),
  }))
  expect(firstPass.map((u) => u.at)).toEqual([1000, 1055])

  // The closing asterisks arrive. The words keep their keys and the slots they were
  // already given — a completed span must not re-queue the text it contains.
  now = 1800
  rerender(<StreamingMarkdown content={'**alpha beta**'} streaming />)
  const secondPass = units(container)
    .filter((node) => node.tagName === 'SPAN')
    .map((node) => ({ key: node.dataset.streamWord, at: revealAt(node) }))
  expect(secondPass).toEqual(firstPass)
  expectOrdered(container)
})

it('closes emphasis around words and math without re-queueing what it holds', () => {
  let now = 1000
  vi.spyOn(performance, 'now').mockImplementation(() => now)
  const { container, rerender } = render(<StreamingMarkdown content={'a**b*c $x$'} streaming />)
  // While the emphasis is open the whole run is one literal word; once it closes, that
  // word splits into `a`, `b`, `c`. What must survive the re-parse:
  //  - the surviving word keeps its key and its slot (a re-queued word would flash);
  //  - the equation keeps its order key and its slot;
  //  - the fresh words get fresh slots from now, in reading order.
  const first = units(container).map((node) => ({
    key: node.dataset.streamWord,
    at: revealAt(node),
  }))
  expect(first.map((u) => u.key)).toEqual(['stream-0', 'equation-0'])

  now = 1800
  rerender(<StreamingMarkdown content={'a**b*c $x$***'} streaming />)
  const second = units(container).map((node) => ({
    key: node.dataset.streamWord,
    at: revealAt(node),
  }))
  expect(second.map((u) => u.key)).toEqual(['stream-0', 'stream-3', 'stream-5', 'equation-0'])
  // The first word and the equation were already on the schedule and are not re-queued.
  expect(second[0].at).toBe(1000)
  expect(second[3].at).toBe(1055)
  // The split-off words are new units: their slots come from now, and they stay in
  // reading order with each other.
  expect(second[1].at).toBeGreaterThanOrEqual(1800)
  expect(second[2].at).toBeGreaterThanOrEqual(second[1].at)
})

it('keeps a stable cascade in reading order as a list and math grow', () => {
  let now = 1000
  vi.spyOn(performance, 'now').mockImplementation(() => now)
  const { container, rerender } = render(
    <StreamingMarkdown content={'- one $x$\n- two'} streaming />,
  )
  expectOrdered(container)
  const before = units(container).map((node) => ({
    key: node.dataset.streamWord,
    at: revealAt(node),
  }))

  now += 600
  rerender(<StreamingMarkdown content={'- one $x$\n- two $y$ and more'} streaming />)
  const after = units(container).map((node) => ({
    key: node.dataset.streamWord,
    at: revealAt(node),
  }))
  // Every unit that was already scheduled keeps the slot it was given.
  for (const old of before) {
    const same = after.find((unit) => unit.key === old.key)
    expect(same, `unit ${old.key} was re-queued`).toBeDefined()
    expect(same!.at).toBe(old.at)
  }
  expectOrdered(container)
})

it('marks code, tables, rules, and checkboxes as single units', () => {
  let now = 1000
  vi.spyOn(performance, 'now').mockImplementation(() => now)
  const content = [
    'before `code word` after',
    '',
    '```js',
    'const x = 1',
    '```',
    '',
    '| a | b |',
    '| - | - |',
    '| 1 | 2 |',
    '',
    '---',
    '',
    '- [ ] task item',
  ].join('\n')
  const { container, rerender } = render(<StreamingMarkdown content={content} streaming />)
  const all = units(container)
  const keys = all.map((node) => node.dataset.streamWord)
  expect(keys).toContain('code-0')
  expect(keys).toContain('code-1')
  expect(keys).toContain('table-0')
  expect(keys).toContain('rule-0')
  expect(keys).toContain('control-0')
  expect(keys).toContain('item-0')

  // Each visual is one unit with no split inside it.
  for (const tagName of ['pre', 'code', 'table']) {
    const el = container.querySelector<HTMLElement>(
      `[data-stream-word="${tagName === 'pre' ? 'code-1' : tagName === 'table' ? 'table-0' : 'code-0'}"]`,
    )!
    expect(el?.querySelectorAll('[data-stream-word]')).toHaveLength(0)
  }
  const inlineCode = all.find((node) => node.dataset.streamWord === 'code-0')!
  expect(inlineCode.textContent).toBe('code word')
  const checkbox = all.find((node) => node.dataset.streamWord === 'control-0')!
  expect(checkbox.tagName).toBe('INPUT')
  expect(checkbox.getAttribute('type')).toBe('checkbox')

  // The cascade never lets a visual land before the prose ahead of it.
  expectOrdered(container)
  const proseBeforeCode = all.filter((node) => node.tagName === 'SPAN')
  const codeUnit = all.find((node) => node.dataset.streamWord === 'code-1')!
  const before = all[all.indexOf(codeUnit) - 1]
  expect(revealAt(before)).toBeLessThanOrEqual(revealAt(codeUnit))
  expect(proseBeforeCode.length).toBeGreaterThanOrEqual(4)

  // Growing the code block never replays the block or re-queues its slot.
  now = 1400
  rerender(
    <StreamingMarkdown
      content={content.replace('const x = 1', 'const x = 1\nconst y = 2')}
      streaming
    />,
  )
  const grown = units(container).find((node) => node.dataset.streamWord === 'code-1')!
  expect(revealAt(grown)).toBe(revealAt(codeUnit))
  expectOrdered(container)
})

it('gives each list marker the deadline of its own first word', () => {
  let now = 1000
  vi.spyOn(performance, 'now').mockImplementation(() => now)
  const { container } = render(<StreamingMarkdown content={'- outer\n  - inner word'} streaming />)
  const all = units(container)
  const byKey = new Map(all.map((node) => [node.dataset.streamWord, node]))
  const outerItem = byKey.get('item-0')!
  const innerItem = byKey.get('item-1')!
  const outerWord = all.find((node) => node.textContent === 'outer')!
  const innerWord = all.find((node) => node.textContent === 'inner')!
  // The outer marker waits for its own first word, not for the nested item; the inner
  // marker waits for its own first word.
  expect(revealAt(outerItem)).toBe(revealAt(outerWord))
  expect(revealAt(innerItem)).toBe(revealAt(innerWord))
  expectOrdered(container)
})

it('clears the schedule when the message generation changes', () => {
  let now = 1000
  vi.spyOn(performance, 'now').mockImplementation(() => now)
  const { container, rerender } = render(
    <StreamingMarkdown content="one two three" streaming generation="message-1:attempt-1" />,
  )
  expect(units(container).map(revealAt)).toEqual([1000, 1055, 1110])

  // A retry replaces the answer in place: its words start a fresh cascade rather than
  // inheriting the slots of the answer they replace.
  now = 1100
  rerender(
    <StreamingMarkdown
      content="different answer words"
      streaming
      generation="message-1:attempt-2"
    />,
  )
  expect(units(container).map(revealAt)).toEqual([1100, 1155, 1210])
  expectOrdered(container)
})

describe('schedule comparison: character, word, and burst feeds of one source', () => {
  const SOURCE = [
    'A short answer.',
    '',
    '- one $\\frac{1}{2}$ and more',
    '- two words here',
    '',
    '```js',
    'const x = 1',
    '```',
    '',
    'Done here.',
  ].join('\n')

  function feeds(): { name: string; prefixes: string[] }[] {
    const char: string[] = []
    for (let i = 1; i <= SOURCE.length; i += 1) char.push(SOURCE.slice(0, i))
    const word: string[] = []
    let acc = ''
    for (const line of SOURCE.split('\n')) {
      for (const piece of line.split(/(\s+)/)) {
        if (piece) acc += piece
      }
      acc += '\n'
      word.push(acc)
    }
    word[word.length - 1] = SOURCE
    return [
      { name: 'character', prefixes: char },
      { name: 'word', prefixes: word },
      { name: 'burst', prefixes: [SOURCE] },
    ]
  }

  for (const { name, prefixes } of feeds()) {
    it(`keeps reading order and a bounded backlog under the ${name} feed`, () => {
      let now = 1000
      vi.spyOn(performance, 'now').mockImplementation(() => now)
      const { container, rerender } = render(<StreamingMarkdown content="" streaming />)
      for (const prefix of prefixes) {
        rerender(<StreamingMarkdown content={prefix} streaming />)
        expectOrdered(container)
        // The backlog stays bounded: nothing is queued further than half a second after
        // the text that carries it has been received.
        const tail = units(container).at(-1)!
        expect(revealAt(tail) - now).toBeLessThanOrEqual(500)
        now += 5
      }
    })
  }

  it('ends with the same text as the static render of the same source', () => {
    let now = 1000
    vi.spyOn(performance, 'now').mockImplementation(() => now)
    const { container } = render(<StreamingMarkdown content={SOURCE} streaming />)
    const streamedText = container.textContent

    const staticContainer = document.createElement('div')
    const staticRender = render(<StreamingMarkdown content={SOURCE} />, {
      container: staticContainer,
    })
    now = 2000
    // The static render settles instantly and carries no units.
    expect(units(staticContainer)).toHaveLength(0)
    expect(staticRender.container.textContent).toBe(streamedText)
  })
})
