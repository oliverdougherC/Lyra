import { Schema } from '@milkdown/kit/prose/model'
import { describe, expect, it } from 'vitest'

import {
  findQuote,
  flattenDoc,
  resolveAnchors,
  stripMarkdownQuote,
} from '@/components/drafts/comment-highlights'

/**
 * The smallest schema the anchoring asks anything of: blocks and text. The resolution
 * only reads text content and positions, which this shares with Milkdown's schema.
 */
const schema = new Schema({
  nodes: {
    doc: { content: 'block+' },
    paragraph: { group: 'block', content: 'text*' },
    heading: { group: 'block', content: 'text*' },
    text: {},
  },
})

function doc(...blocks: Array<['paragraph' | 'heading', string]>) {
  return schema.nodes.doc.create(
    null,
    blocks.map(([kind, text]) => schema.nodes[kind].create(null, text ? schema.text(text) : null)),
  )
}

describe('flattenDoc', () => {
  it('maps every character back to its document position', () => {
    const document = doc(['heading', 'Results'], ['paragraph', 'The estimator converges.'])

    const flat = flattenDoc(document)

    expect(flat.text).toBe('Results\nThe estimator converges.')
    // Every mapped position must resolve to the same character in the real document.
    const index = flat.text.indexOf('estimator')
    const from = flat.positions[index]
    expect(document.textBetween(from, from + 'estimator'.length)).toBe('estimator')
  })
})

describe('stripMarkdownQuote', () => {
  it('sheds the syntax the renderer sheds', () => {
    expect(stripMarkdownQuote('## Methods')).toBe('Methods')
    expect(stripMarkdownQuote('the **bold** claim with `code`')).toBe('the bold claim with code')
    expect(stripMarkdownQuote('- first item\n- second item')).toBe('first item\nsecond item')
    expect(stripMarkdownQuote('see [the appendix](https://x.test/a)')).toBe('see the appendix')
    expect(stripMarkdownQuote('\\[TODO: describe the rig]')).toBe('[TODO: describe the rig]')
    expect(stripMarkdownQuote('> a quoted line')).toBe('a quoted line')
  })
})

describe('findQuote', () => {
  const document = doc(
    ['heading', 'Results'],
    ['paragraph', 'The estimator converges in probability.'],
    ['paragraph', 'It is unbiased and consistent.'],
  )
  const flat = flattenDoc(document)

  it('finds a plain quote and returns real document positions', () => {
    const range = findQuote(flat, 'converges in probability')

    expect(range).not.toBeNull()
    expect(document.textBetween(range!.from, range!.to)).toBe('converges in probability')
  })

  it('finds a markdown-dressed quote against the rendered text', () => {
    const range = findQuote(flat, 'The **estimator** converges')

    expect(range).not.toBeNull()
    expect(document.textBetween(range!.from, range!.to)).toBe('The estimator converges')
  })

  it('tolerates reflowed whitespace on either side', () => {
    const range = findQuote(flat, 'probability.   It is')

    expect(range).not.toBeNull()
    expect(document.textBetween(range!.from, range!.to, '\n')).toBe('probability.\nIt is')
  })

  it('normalizes case and common Unicode punctuation like the server', () => {
    const punctuated = doc(['paragraph', 'The “Estimator” is robust—under noise.'])

    const range = findQuote(flattenDoc(punctuated), 'the "estimator" is robust-under noise')

    expect(range).not.toBeNull()
    expect(punctuated.textBetween(range!.from, range!.to)).toContain('Estimator')
  })

  it('conservatively recovers a long quote with one copied word wrong', () => {
    const edited = doc([
      'paragraph',
      'The estimator quickly converges under every tested sampling condition.',
    ])

    const range = findQuote(
      flattenDoc(edited),
      'The estimator steadily converges under every tested sampling condition.',
    )

    expect(range).not.toBeNull()
    expect(edited.textBetween(range!.from, range!.to)).toContain('quickly converges')
  })

  it('answers null for a quote the document does not contain', () => {
    expect(findQuote(flat, 'a passage that was deleted')).toBeNull()
    expect(findQuote(flat, '')).toBeNull()
  })
})

describe('resolveAnchors', () => {
  it('decorates each resolvable thread with its severity and id, skipping the rest', () => {
    const document = doc(
      ['heading', 'Results'],
      ['paragraph', 'The estimator converges in probability.'],
    )

    const set = resolveAnchors(document, [
      { id: 7, quote: 'converges in probability', severity: 'critical' },
      { id: 8, quote: 'a deleted passage', severity: 'major' },
      { id: 9, quote: '## Results', severity: null },
    ])

    const decorations = set.find()
    expect(
      decorations
        .map((deco) => deco.spec.commentId)
        .filter((id): id is number => typeof id === 'number')
        .sort(),
    ).toEqual([7, 9])
    expect(
      decorations
        .map((deco) => deco.spec.gutterCommentId)
        .filter((id): id is number => typeof id === 'number')
        .sort(),
    ).toEqual([7, 9])
    const critical = decorations.find((deco) => deco.spec.commentId === 7)!
    expect(document.textBetween(critical.from, critical.to)).toBe('converges in probability')
  })
})
