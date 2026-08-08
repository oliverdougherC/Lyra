import type { Node } from '@milkdown/kit/prose/model'
import { Plugin, PluginKey } from '@milkdown/kit/prose/state'
import { Decoration, DecorationSet } from '@milkdown/kit/prose/view'
import { $prose } from '@milkdown/kit/utils'

// The writer-facing syntax is `[source:12]`; the older export syntax remains readable
// while stored drafts converge, so neither form turns back into raw punctuation.
const CITATION = /\[(?:source:|@lyra(?::|-))(\d+)\]/g
const key = new PluginKey<DecorationSet>('lyra-draft-citations')

/** Render stable ledger markers as source chips without changing serialized markdown. */
export function citationDecorations(doc: Node): DecorationSet {
  const decorations: Decoration[] = []
  doc.descendants((node, pos) => {
    if (!node.isText || !node.text) return true
    for (const match of node.text.matchAll(CITATION)) {
      if (match.index === undefined) continue
      const sourceId = Number(match[1])
      if (!Number.isSafeInteger(sourceId) || sourceId < 1) continue
      decorations.push(
        Decoration.inline(
          pos + match.index,
          pos + match.index + match[0].length,
          {
            'class': 'draft-citation',
            'data-source-id': String(sourceId),
            'title': `Open source ${sourceId}`,
            'role': 'button',
            'tabindex': '0',
          },
          { sourceId },
        ),
      )
    }
    return true
  })
  return DecorationSet.create(doc, decorations)
}

export const citationHighlightsPlugin = $prose(
  () =>
    new Plugin<DecorationSet>({
      key,
      state: {
        init: (_config, state) => citationDecorations(state.doc),
        apply: (transaction, current) =>
          transaction.docChanged
            ? citationDecorations(transaction.doc)
            : current.map(transaction.mapping, transaction.doc),
      },
      props: {
        decorations: (state) => key.getState(state),
      },
    }),
)
