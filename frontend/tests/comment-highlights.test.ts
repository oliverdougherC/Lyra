import { Schema } from '@milkdown/kit/prose/model'
import type { Node } from '@milkdown/kit/prose/model'
import { EditorState, type Transaction } from '@milkdown/kit/prose/state'
import type { Decoration, DecorationSet } from '@milkdown/kit/prose/view'
import { describe, expect, it } from 'vitest'

import {
  buildDocIndex,
  commentPluginKey,
  createCommentPlugin,
  findQuote,
  findQuoteIn,
  flattenDoc,
  resolveAnchors,
  stripMarkdownQuote,
  type AnchorThread,
  type ResolvedAnchor,
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

describe('the shared pass index (PLA-512)', () => {
  const document = doc(
    ['paragraph', 'The estimator converges in probability.'],
    ['paragraph', 'It is unbiased and consistent.'],
  )
  const flat = flattenDoc(document)

  it('normalizes the document once per pass, no matter how many threads hit or miss', () => {
    const index = buildDocIndex(flat)
    const hits = ['converges in probability', 'It is unbiased', 'unbiased and consistent', 'converges', 'probability']
    const misses = ['a passage that was deleted', 'another absent passage', 'a third missing one']

    for (const quote of hits) expect(findQuoteIn(index, quote)).not.toBeNull()
    for (const quote of misses) expect(findQuoteIn(index, quote)).toBeNull()

    // Eight lookups, three of them full misses: the whitespace form was built once and
    // the canonical form at most once, not once per thread.
    expect(index.builds.space).toBe(1)
    expect(index.builds.canonical).toBe(1)
  })

  it('never builds the canonical form when every thread matches the primary form', () => {
    const index = buildDocIndex(flat)
    for (const quote of ['converges in probability', 'unbiased and consistent', 'converges']) {
      expect(findQuoteIn(index, quote)).not.toBeNull()
    }
    expect(index.builds.space).toBe(1)
    expect(index.builds.canonical).toBe(0)
  })

  it('agrees with the throwaway findQuote index on exact, normalized, fuzzy, and missing quotes', () => {
    const quotes = [
      'converges in probability', // exact
      'The **estimator** converges', // markdown-dressed
      'THE "ESTIMATOR" IS ROBUST-UNDER NOISE', // case/punctuation canonical form
      'The estimator steadily converges under every tested sampling condition.', // fuzzy
      'a passage that was deleted', // missing
    ]
    const longDoc = doc(['paragraph', 'The estimator quickly converges under every tested sampling condition.'])
    for (const [source, quote] of [
      [flat, quotes[0]],
      [flat, quotes[1]],
      [flattenDoc(doc(['paragraph', 'The “Estimator” is robust—under noise.'])), quotes[2]],
      [flattenDoc(longDoc), quotes[3]],
      [flat, quotes[4]],
    ] as Array<[ReturnType<typeof flattenDoc>, string]>) {
      expect(findQuoteIn(buildDocIndex(source), quote)).toEqual(findQuote(source, quote))
    }
  })
})

describe('comment plugin state, driven through a real EditorState', () => {
  const document = doc(
    ['paragraph', 'The estimator converges in probability.'],
    ['paragraph', 'It is unbiased and consistent.'],
  )
  const threads: AnchorThread[] = [
    { id: 7, quote: 'converges in probability', severity: 'critical' },
    { id: 9, quote: 'unbiased and consistent', severity: null },
    { id: 11, quote: 'a passage that was deleted', severity: 'major' },
  ]

  type PluginState = {
    threads: AnchorThread[]
    flashId: number | null
    ranges: ResolvedAnchor[]
    decorations: DecorationSet
    resolutions: number
    rebuilds: number
  }

  /**
   * The inline attrs of a decoration (`class` and friends live on the decoration's type,
   * not in `spec`). The type field is internal to prosemirror-view, hence the cast.
   */
  const attrsOf = (deco: Decoration): Record<string, unknown> =>
    ((deco as unknown as { type?: { attrs?: Record<string, unknown> } }).type?.attrs ?? {})

  class CommentHarness {
    private plugin = createCommentPlugin()
    state: EditorState

    constructor(doc: Node) {
      this.state = EditorState.create({ doc, plugins: [this.plugin] })
    }

    get ps(): PluginState {
      // init runs on every state, so the plugin state exists from the first access on.
      return commentPluginKey.getState(this.state)!
    }

    get doc(): Node {
      return this.state.doc
    }

    set(threads: AnchorThread[]): void {
      this.state = this.state.apply(
        this.state.tr.setMeta(commentPluginKey, { type: 'set', threads }),
      )
    }

    flash(id: number): void {
      this.state = this.state.apply(this.state.tr.setMeta(commentPluginKey, { type: 'flash', id }))
    }

    unflash(id: number): void {
      this.state = this.state.apply(this.state.tr.setMeta(commentPluginKey, { type: 'unflash', id }))
    }

    edit(mutate: (tr: Transaction) => void): void {
      const tr = this.state.tr
      mutate(tr)
      this.state = this.state.apply(tr)
    }

    inlines(): ReturnType<DecorationSet['find']> {
      return this.ps.decorations.find().filter((deco) => deco.to > deco.from)
    }
  }

  it('resolves once per set and stores the ranges', () => {
    const harness = new CommentHarness(document)
    harness.set(threads)

    expect(harness.ps.resolutions).toBe(1)
    expect(harness.ps.ranges.map((range) => range.id)).toEqual([7, 9])
    expect(harness.inlines()).toHaveLength(2)
  })

  it('flashes and unflashes from stored ranges without a single re-resolution', () => {
    const harness = new CommentHarness(document)
    harness.set(threads)

    harness.flash(7)
    expect(harness.ps.resolutions).toBe(1) // unchanged
    expect(harness.ps.rebuilds).toBe(1)
    expect(harness.ps.flashId).toBe(7)
    expect(harness.ps.ranges.map((range) => range.id)).toEqual([7, 9])
    const flashed = harness.inlines().find((deco) => deco.spec.commentId === 7)!
    expect(String(attrsOf(flashed).class)).toContain('comment-anchor--flash')
    const unflashed = harness.inlines().find((deco) => deco.spec.commentId === 9)!
    expect(String(attrsOf(unflashed).class)).not.toContain('comment-anchor--flash')

    harness.unflash(7)
    expect(harness.ps.rebuilds).toBe(2)
    expect(harness.ps.flashId).toBeNull()
    expect(harness.inlines().every((deco) => !String(attrsOf(deco).class).includes('--flash'))).toBe(
      true,
    )
  })

  it('treats an unflash for a non-flashed id as a no-op', () => {
    const harness = new CommentHarness(document)
    harness.set(threads)
    const before = harness.ps
    harness.unflash(9)
    expect(harness.ps).toBe(before)
    expect(harness.ps.rebuilds).toBe(0)
  })

  it('rides the mapping through a local edit instead of re-resolving', () => {
    const harness = new CommentHarness(document)
    harness.set(threads)
    const originalFrom = harness.ps.ranges.find((range) => range.id === 7)!.from

    // Insert at the very start of the first paragraph's text: every anchor in it shifts.
    harness.edit((tr) => tr.insertText('Hello ', 1))

    expect(harness.ps.resolutions).toBe(1) // unchanged: mapping only
    const moved = harness.ps.ranges.find((range) => range.id === 7)!
    expect(moved.from).toBe(originalFrom + 'Hello '.length)
    expect(harness.doc.textBetween(moved.from, moved.to)).toBe('converges in probability')
  })

  it('re-resolves everything exactly once when an edit swallows an anchor', () => {
    const harness = new CommentHarness(document)
    harness.set(threads)
    expect(harness.ps.resolutions).toBe(1)

    // Delete the whole first paragraph's text: thread 7's passage is gone, thread 9's
    // passage survives and must be re-found at its shifted position.
    const first = harness.doc.firstChild!
    harness.edit((tr) => tr.delete(1, 1 + first.content.size))

    expect(harness.ps.resolutions).toBe(2) // exactly one full re-resolution
    expect(harness.ps.ranges.map((range) => range.id)).toEqual([9])
    const survivor = harness.ps.ranges[0]
    expect(harness.doc.textBetween(survivor.from, survivor.to)).toBe('unbiased and consistent')
  })

  it('maps the edit before styling when a flash rides in the same transaction', () => {
    const harness = new CommentHarness(document)
    harness.set(threads)

    // One transaction carries both the edit and the flash: the plugin must map (or
    // re-resolve) first, then style - a flash that ran against the pre-edit ranges would
    // paint the anchor where the text used to be.
    const tr = harness.state.tr
    tr.insertText('Hello ', 1)
    tr.setMeta(commentPluginKey, { type: 'flash', id: 7 })
    harness.state = harness.state.apply(tr)

    expect(harness.ps.resolutions).toBe(1) // the edit mapped, it did not re-resolve
    expect(harness.ps.rebuilds).toBe(1) // the flash rebuilt from the stored ranges
    expect(harness.ps.flashId).toBe(7)
    const flashed = harness.inlines().find((deco) => deco.spec.commentId === 7)!
    expect(harness.doc.textBetween(flashed.from, flashed.to)).toBe('converges in probability')
    expect(flashed.from).toBeGreaterThanOrEqual(1 + 'Hello '.length) // past the insert
    expect(String(attrsOf(flashed).class)).toContain('comment-anchor--flash')
  })
})
