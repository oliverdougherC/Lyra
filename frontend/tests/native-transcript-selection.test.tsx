import { describe, expect, it } from 'vitest'

import { selectionFromTranscript } from '@/native-transcript'
import type { NativeChatSnapshot } from '@/lib/native-chat'

function fixture() {
  const rows = Array.from({ length: 34 }, (_, i) => ({
    key: `row-${i}`,
    message: { id: i, role: 'assistant' as const, content: `answer ${i}` },
    startsTimeGap: false,
  }))
  rows.push({
    key: 'opt-42',
    message: { id: -2, role: 'assistant', content: 'live answer' },
    startsTimeGap: false,
    streaming: true,
    generation: 'live-42',
  } as (typeof rows)[number])
  const snapshot = {
    hostId: 'host',
    version: 9,
    scope: 'one',
    rows,
    agent: true,
    dark: false,
    liveGeneration: 'live-42',
  } as NativeChatSnapshot
  const parent = document.createElement('main')
  for (const row of rows) {
    const wrapper = document.createElement('div')
    wrapper.dataset.nativeRowKey = row.key
    const content = document.createElement('div')
    content.className = 'assistant-content'
    content.textContent = row.message.content
    wrapper.append(content)
    parent.append(wrapper)
  }
  document.body.append(parent)
  const select = (key: string) => {
    const node = parent.querySelector(
      `[data-native-row-key="${key}"] .assistant-content`,
    )!.firstChild!
    const range = document.createRange()
    range.setStart(node, 1)
    range.setEnd(node, 3)
    const selection = window.getSelection()!
    selection.removeAllRanges()
    selection.addRange(range)
    return selection
  }
  return { snapshot, parent, select }
}

describe('native transcript selection ownership', () => {
  it('finds the actual row beyond the first 32 and does not label an old answer live', () => {
    const { snapshot, parent, select } = fixture()
    expect(selectionFromTranscript(snapshot, select('row-33'))).toEqual({
      kind: 'selection',
      hostId: 'host',
      scope: 'one',
      version: 9,
      rowKey: 'row-33',
      anchor: 1,
      focus: 3,
    })
    parent.remove()
  })

  it('reports the live row generation and clears when selection leaves the reply', () => {
    const { snapshot, parent, select } = fixture()
    expect(selectionFromTranscript(snapshot, select('opt-42'))).toEqual({
      kind: 'selection',
      hostId: 'host',
      scope: 'one',
      version: 9,
      rowKey: 'opt-42',
      anchor: 1,
      focus: 3,
      generation: 'live-42',
    })
    window.getSelection()?.removeAllRanges()
    expect(selectionFromTranscript(snapshot, window.getSelection())).toEqual({
      kind: 'selection',
      hostId: 'host',
      scope: 'one',
      version: 9,
      rowKey: null,
    })
    parent.remove()
  })
})
