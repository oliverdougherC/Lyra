import { Schema } from '@milkdown/kit/prose/model'
import { describe, expect, it } from 'vitest'

import { citationDecorations } from '@/components/drafts/citation-highlights'

const schema = new Schema({
  nodes: {
    doc: { content: 'block+' },
    paragraph: { group: 'block', content: 'text*' },
    text: {},
  },
})

function paragraph(text: string) {
  return schema.nodes.doc.create(null, [schema.nodes.paragraph.create(null, schema.text(text))])
}

describe('citationDecorations', () => {
  it('turns stable writer and legacy export markers into source links', () => {
    const doc = paragraph('Claim [source:12], earlier claim [@lyra:3].')

    const decorations = citationDecorations(doc).find()

    expect(decorations.map((one) => one.spec.sourceId)).toEqual([12, 3])
    expect(doc.textBetween(decorations[0].from, decorations[0].to)).toBe('[source:12]')
  })

  it('leaves malformed or non-positive ids as ordinary text', () => {
    const doc = paragraph('Not citations: [source:x] and [source:0].')

    expect(citationDecorations(doc).find()).toHaveLength(0)
  })
})
