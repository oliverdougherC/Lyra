import { Node as ProseMirrorNode, Schema } from '@milkdown/kit/prose/model'
import { EditorState } from '@milkdown/kit/prose/state'
import { expect, it, vi } from 'vitest'

import { citationDecorations, createCitationPlugin } from '@/components/drafts/citation-highlights'

it('does not reread every untouched citation while editing one paragraph', () => {
  const schema = new Schema({
    nodes: {
      doc: { content: 'paragraph+' },
      paragraph: { content: 'text*' },
      text: {},
    },
  })
  const paragraphs = Array.from({ length: 200 }, (_, index) =>
    schema.node(
      'paragraph',
      null,
      schema.text(`Text [source:${index + 1}] in paragraph ${index}.`),
    ),
  )
  const doc = schema.node('doc', null, paragraphs)
  const plugin = createCitationPlugin()
  const state = EditorState.create({ doc, plugins: [plugin] })
  const before = plugin.getState(state)!.stats
  const rangeReads = vi.spyOn(ProseMirrorNode.prototype, 'textBetween')
  const position = paragraphs.slice(0, 100).reduce((sum, node) => sum + node.nodeSize, 0) + 2
  const next = state.apply(state.tr.insertText('x', position))
  const current = plugin.getState(next)!

  expect(rangeReads.mock.calls.length).toBeLessThanOrEqual(2)
  expect(current.stats.nodesVisited - before.nodesVisited).toBeLessThanOrEqual(2)
  const ranges = (set: ReturnType<typeof citationDecorations>) =>
    set.find().map((deco) => [deco.from, deco.to, deco.spec.sourceId])
  expect(ranges(current.decorations)).toEqual(ranges(citationDecorations(next.doc)))
})
