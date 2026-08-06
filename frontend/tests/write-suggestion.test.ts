import { Schema } from '@milkdown/kit/prose/model'
import { EditorState, TextSelection } from '@milkdown/kit/prose/state'
import { describe, expect, it } from 'vitest'

import { buildWriteRequest, gatherWriteContext } from '@/components/drafts/write-suggestion'

/**
 * The smallest schema the gatherer asks anything of: a document of paragraphs and
 * headings. The editor's real schema is Milkdown's, but the gatherer only reads node
 * names, text, and top-level structure, which this shares with it.
 */
const schema = new Schema({
  nodes: {
    doc: { content: 'block+' },
    paragraph: { group: 'block', content: 'text*' },
    heading: { group: 'block', content: 'text*' },
    text: {},
  },
})

function block(kind: 'paragraph' | 'heading', text: string) {
  return schema.nodes[kind].create(null, text ? schema.text(text) : null)
}

function stateWith(blocks: ReturnType<typeof block>[], anchor: number): EditorState {
  const doc = schema.nodes.doc.create(null, blocks)
  return EditorState.create({ doc, selection: TextSelection.create(doc, anchor) })
}

describe('gatherWriteContext', () => {
  it('names the nearest heading at or before the caret', () => {
    // [# Results][The estimator converges...][It is unbiased.]
    // heading spans 0..8, the first paragraph 8..31, so 10 lands inside it.
    const state = stateWith(
      [
        block('heading', 'Results'),
        block('paragraph', 'The estimator converges in probability.'),
        block('paragraph', 'It is unbiased.'),
      ],
      10,
    )

    const ctx = gatherWriteContext(state)

    expect(ctx.heading).toBe('Results')
    expect(ctx.selection).toBe('')
    expect(ctx.nearby).toContain('The estimator converges in probability.')
  })

  it('reports no heading when the caret sits before any', () => {
    const state = stateWith(
      [block('paragraph', 'An opening line.'), block('heading', 'Methods')],
      2,
    )

    expect(gatherWriteContext(state).heading).toBeNull()
  })

  it('carries the selection when there is one', () => {
    // Text begins one position inside the paragraph, so 'middle' sits at 10..16.
    const state = stateWith([block('paragraph', 'pick the middle of this line')], 1)
    const selection = TextSelection.create(state.doc, 10, 16)

    const ctx = gatherWriteContext(EditorState.create({ doc: state.doc, selection }))

    expect(ctx.selection).toBe('middle')
  })

  it('gathers the current block and its predecessor, and no further back', () => {
    const blocks = [
      block('paragraph', 'First.'),
      block('paragraph', 'Second.'),
      block('paragraph', 'Third.'),
    ]
    // Each short paragraph is 8 or 9 positions wide; the caret goes in the last one.
    const doc = schema.nodes.doc.create(null, blocks)
    const state = EditorState.create({ doc, selection: TextSelection.atEnd(doc) })

    const ctx = gatherWriteContext(state)

    expect(ctx.nearby).toBe('Second.\n\nThird.')
  })

  it('caps the surrounding text rather than handing over the whole document', () => {
    const long = 'x'.repeat(800)
    const doc = schema.nodes.doc.create(null, [block('paragraph', long)])
    const state = EditorState.create({ doc, selection: TextSelection.atEnd(doc) })

    expect(gatherWriteContext(state).nearby.length).toBeLessThanOrEqual(600)
  })
})

describe('buildWriteRequest', () => {
  it('keeps the grounding that exists and drops what does not', () => {
    const request = buildWriteRequest('Expand this', {
      heading: 'Results',
      selection: 'the p-value',
      nearby: '',
    })

    expect(request).toEqual({
      instruction: 'Expand this',
      heading: 'Results',
      selection: 'the p-value',
      nearby: undefined,
    })
  })

  it('sends the bare instruction when nothing was gathered', () => {
    expect(
      buildWriteRequest('Write an introduction', { heading: null, selection: '', nearby: '' }),
    ).toEqual({
      instruction: 'Write an introduction',
      heading: undefined,
      selection: undefined,
      nearby: undefined,
    })
  })
})
