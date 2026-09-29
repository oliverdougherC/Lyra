/**
 * The citation-decoration plugin, driven through a real ProseMirror EditorState (PLA-512).
 *
 * Every test below ends by checking the plugin's decorations against a full-document scan
 * of the resulting document (`citationDecorations` is the oracle): the incremental pass
 * may visit fewer nodes, but it may never produce a stale, duplicated, or misplaced
 * decoration. The stats on the plugin state are the measured cost: which path ran, and
 * how many text nodes and characters it read.
 */
import { Mark, Schema, Slice } from '@milkdown/kit/prose/model'
import type { MarkType, Node } from '@milkdown/kit/prose/model'
import { EditorState, type Transaction } from '@milkdown/kit/prose/state'
import { history, redo, undo } from '@milkdown/kit/prose/history'
import type { Decoration } from '@milkdown/kit/prose/view'
import { describe, expect, it } from 'vitest'

import {
  citationDecorations,
  citationPluginKey,
  createCitationPlugin,
  type CitationPluginState,
  type CitationScanStats,
} from '@/components/drafts/citation-highlights'

/**
 * The smallest schema the anchoring asks anything of: blocks and text, plus a mark so a
 * mark-only transaction can be exercised. This shares the shape of Milkdown's schema.
 */
const schema = new Schema({
  nodes: {
    doc: { content: 'block+' },
    paragraph: { group: 'block', content: 'text*' },
    text: {},
  },
  marks: {
    em: {},
  },
})

function docOf(...paragraphs: string[]): Node {
  return schema.nodes.doc.create(
    null,
    paragraphs.map((text) => schema.nodes.paragraph.create(null, schema.text(text))),
  )
}

// This prosemirror-model version's d.ts omits the Mark constructor argument, but the
// runtime takes the mark type: cast once, here where marks are created.
const emMark = (): Mark => new (Mark as unknown as new (type: MarkType) => Mark)(schema.marks.em)

/**
 * A live editor state carrying the production citation plugin (plus optional extra
 * plugins), with helpers to drive transactions and to read the plugin's state and stats.
 */
class CitationHarness {
  private plugin = createCitationPlugin()
  state: EditorState

  constructor(doc: Node, extraPlugins: readonly unknown[] = []) {
    this.state = EditorState.create({
      doc,
      plugins: [this.plugin, ...(extraPlugins as (typeof this.plugin)[])],
    })
  }

  get ps(): CitationPluginState {
    // init runs on every state, so the plugin state exists from the first access on.
    return citationPluginKey.getState(this.state)!
  }

  get doc(): Node {
    return this.state.doc
  }

  /** Apply a mutation to a fresh transaction against the current state. */
  apply(mutate: (tr: Transaction) => void): void {
    const tr = this.state.tr
    mutate(tr)
    this.state = this.state.apply(tr)
  }

  /** The production full scan of the current document: the correctness oracle. */
  oracle(): Decoration[] {
    return citationDecorations(this.doc).find()
  }

  actual(): Decoration[] {
    return this.ps.decorations.find()
  }

  /** The plugin set must equal the full-scan set, position and source id for position. */
  expectMatchesOracle(): void {
    const summarize = (decos: Decoration[]) =>
      decos.map((deco) => [deco.from, deco.to, deco.spec.sourceId])
    expect(summarize(this.actual())).toEqual(summarize(this.oracle()))
  }
}

describe('citationDecorations (full scan)', () => {
  it('turns stable writer and legacy export markers into source links', () => {
    const doc = docOf('Claim [source:12], earlier claim [@lyra:3].')

    const decorations = citationDecorations(doc).find()

    expect(decorations.map((one) => one.spec.sourceId)).toEqual([12, 3])
    expect(doc.textBetween(decorations[0].from, decorations[0].to)).toBe('[source:12]')
  })

  it('leaves malformed or non-positive ids as ordinary text', () => {
    const doc = docOf('Not citations: [source:x] and [source:0].')

    expect(citationDecorations(doc).find()).toHaveLength(0)
  })
})

describe('citation plugin: measured scans', () => {
  it('performs one full scan at init and none while idle', () => {
    const harness = new CitationHarness(docOf('Claim [source:12].'))
    const stats = harness.ps.stats

    expect(stats.fullScans).toBe(1)
    expect(stats.incrementalScans).toBe(0)
    expect(stats.nodesVisited).toBe(1)

    // A step-free transaction is an idle editor: the set rides the mapping, no scan.
    const idle = harness.state.tr
    harness.state = harness.state.apply(idle)
    expect(harness.ps.stats).toEqual(stats)
    harness.expectMatchesOracle()
  })

  it('visits only the edited text node for a local edit in a long draft', () => {
    const paragraphs = Array.from(
      { length: 150 },
      (_, i) => `Paragraph ${i} with ordinary writing.`,
    )
    paragraphs[10] = 'Claim [source:12] in paragraph ten.'
    paragraphs[120] = 'Later claim [@lyra:77] down here.'
    const harness = new CitationHarness(docOf(...paragraphs))
    const before: CitationScanStats = { ...harness.ps.stats }
    expect(harness.actual()).toHaveLength(2)

    // Type at the very end of the last paragraph: a local edit one hundred lines away
    // from every marker.
    const last = paragraphs[149].length
    const end = harness.doc.content.size - 1
    harness.apply((tr) => tr.insertText('!', end))

    const stats = harness.ps.stats
    expect(stats.fullScans).toBe(before.fullScans) // still 1: only the init scan
    expect(stats.incrementalScans).toBe(before.incrementalScans + 1)
    expect(stats.nodesVisited - before.nodesVisited).toBe(1) // only the edited text node
    // The node is rescanned after the insertion, so the scan reads its new length.
    expect(stats.charsScanned - before.charsScanned).toBe(last + 1)
    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(2)
  })

  it('keeps markers alive through edits that do not touch them, without rescanning', () => {
    const harness = new CitationHarness(
      docOf('Claim [source:12] here.', 'Another [source:99] there.'),
    )
    const before = harness.ps.stats

    // Insert at the end of the second paragraph, away from either marker.
    harness.apply((tr) => tr.insertText(' right there', tr.doc.content.size - 1))

    expect(harness.ps.stats.nodesVisited - before.nodesVisited).toBeGreaterThan(0)
    // The first paragraph's node was not among those visited: its marker was mapped,
    // not rescanned, which the visited-node count proves (only node 2 was read).
    expect(harness.ps.stats.nodesVisited - before.nodesVisited).toBe(1)
    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(2)
  })

  it('finds a marker as it is completed across transactions', () => {
    const harness = new CitationHarness(docOf('Claim [source:1'))
    expect(harness.actual()).toHaveLength(0)
    harness.expectMatchesOracle()

    // The rest of the marker arrives in a later transaction; the whole text node is
    // rescanned and the completed marker appears.
    harness.apply((tr) => tr.insertText('2]', harness.doc.content.size - 1))

    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(1)
    expect(harness.doc.textBetween(harness.actual()[0].from, harness.actual()[0].to)).toBe(
      '[source:12]',
    )
  })

  it('heals a broken marker when the break is deleted (a pure deletion)', () => {
    const harness = new CitationHarness(docOf('Claim [source:1 2] here.'))
    expect(harness.actual()).toHaveLength(0)
    harness.expectMatchesOracle()

    const spaceAt = 'Claim [source:1 '.length
    harness.apply((tr) => tr.delete(spaceAt, spaceAt + 1))

    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(1)
    expect(harness.doc.textBetween(harness.actual()[0].from, harness.actual()[0].to)).toBe(
      '[source:12]',
    )
  })

  it('drops a marker its edit breaks, and restores it on undo/redo', () => {
    const harness = new CitationHarness(docOf('Claim [source:12] here.'), [history()])
    const markerFrom = harness.actual()[0].from
    harness.expectMatchesOracle()

    // Delete the last digit of the id: the marker becomes `[source:1]` - a different,
    // still valid citation - so the old decoration must be replaced, not kept or lost.
    harness.apply((tr) => tr.delete(markerFrom + 9, markerFrom + 10))
    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(1)
    expect(harness.actual()[0].spec.sourceId).toBe(1)

    // `undo`/`redo` are commands: they dispatch the history transaction when one is
    // owed, so run them with a dispatch that applies the result to the state.
    const runCommand = (
      command: (state: EditorState, dispatch?: (tr: Transaction) => void) => boolean,
    ): void => {
      let dispatched: Transaction | null = null
      const ran = command(harness.state, (tr) => {
        dispatched = tr
      })
      expect(ran).toBe(true)
      if (dispatched) harness.state = harness.state.apply(dispatched)
    }

    // Undo restores `[source:12]`; redo applies the change again.
    runCommand(undo)
    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(1)
    expect(harness.actual()[0].spec.sourceId).toBe(12)

    runCommand(redo)
    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(1)
    expect(harness.actual()[0].spec.sourceId).toBe(1)
  })

  it('maps decorations through paragraph split and join', () => {
    const harness = new CitationHarness(docOf('Claim [source:12] here, and more after.'))
    const before = harness.ps.stats
    harness.expectMatchesOracle()

    // Split the paragraph mid-text; the marker keeps its text and its decoration.
    const splitAt = 'Claim [source:12] here'.length
    harness.apply((tr) => tr.split(splitAt))
    expect(harness.ps.stats.incrementalScans).toBe(before.incrementalScans + 1)
    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(1)

    // Join it back: same story in reverse. The join point is the end of the first
    // paragraph, which after the split is the end of the first top-level child.
    const joinAt = harness.doc.firstChild!.nodeSize
    harness.apply((tr) => tr.join(joinAt))
    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(1)
  })

  it('handles paste and multi-step transactions without stale or duplicate decorations', () => {
    const harness = new CitationHarness(docOf('First paragraph.', 'Second paragraph.'))

    // Paste a fragment containing two markers in one step.
    const pasted = docOf('Pasted [source:5] and [@lyra:6] text.')
    const fragment = pasted.firstChild!.content
    harness.apply((tr) => tr.insert(harness.doc.content.size - 1, fragment))
    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(2)

    // A multi-step transaction: two edits in one transaction, one per paragraph.
    const statsBefore = { ...harness.ps.stats }
    harness.apply((tr) => {
      tr.insertText(' more', 5)
      tr.insertText(' more', harness.doc.content.size - 1)
    })
    expect(harness.ps.stats.incrementalScans).toBe(statsBefore.incrementalScans + 1)
    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(2)
  })

  it('survives IME composition: a tentative insert then a replace', () => {
    const harness = new CitationHarness(docOf('Claim '))
    const at = harness.doc.content.size - 1

    // Composition start: the IME's tentative characters land in the document.
    harness.apply((tr) => tr.insertText('x', at))
    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(0)

    // Composition end: the tentative text is replaced by the final conversion, which
    // completes a marker the tentative form did not contain.
    harness.apply((tr) => tr.replaceWith(at, at + 1, schema.text('[source:12]')))
    harness.expectMatchesOracle()
    const deco = harness.actual().find((one) => one.spec.sourceId === 12)
    expect(deco).toBeDefined()
  })

  it('re-finds a marker moved by cut-and-paste in one transaction', () => {
    const harness = new CitationHarness(docOf('Claim [source:12] here.', 'Second paragraph.'))
    const markerFrom = harness.actual()[0].from
    const markerTo = markerFrom + '[source:12]'.length

    // Delete the marker from the first paragraph and paste it into the second.
    harness.apply((tr) => {
      const secondStart = tr.doc.firstChild!.nodeSize
      tr.replace(markerFrom, markerTo)
      tr.insertText('[source:12]', secondStart)
    })

    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(1)
    expect(harness.doc.textBetween(harness.actual()[0].from, harness.actual()[0].to)).toBe(
      '[source:12]',
    )
  })

  it('rescans whole nodes for adjacent text and marks without losing the marker', () => {
    const harness = new CitationHarness(docOf('Claim [source:12] here.'))

    // A mark over the marker text: the marker itself is untouched and must survive.
    const from = harness.actual()[0].from
    const to = harness.actual()[0].to
    // This prosemirror-model version's d.ts omits the Mark constructor argument, but the
    // runtime takes the mark type: cast once, here where it is created.
    harness.apply((tr) =>
      tr.addMark(from, to, new (Mark as unknown as new (type: MarkType) => Mark)(schema.marks.em)),
    )
    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(1)
    expect(harness.actual()[0].from).toBe(from)

    // Typing adjacent to the marker (right before it) shifts but does not break it.
    const markerFrom = harness.actual()[0].from
    harness.apply((tr) => tr.insertText('x', markerFrom))
    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(1)
    expect(harness.actual()[0].from).toBe(markerFrom + 1)
  })

  it('drops a marker a partial mark splits across text nodes, matching the full scan', () => {
    const harness = new CitationHarness(docOf('[source:1]'))
    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(1)

    // A mark over part of the marker splits its text node. The oracle's regex runs per
    // node, so the marker no longer matches a full scan - the plugin must agree and
    // drop the survivor, not keep a decoration the full scan cannot see.
    harness.apply((tr) => tr.addMark(2, 5, emMark()))
    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(0)
  })

  it('keeps a marker whose text node survives a mark add and a mark removal', () => {
    const harness = new CitationHarness(docOf('Claim [source:12] here.'))
    const marker = harness.actual()[0]
    expect(marker).toBeDefined()

    // A mark over the whole marker keeps its text in one node: still highlighted...
    harness.apply((tr) => tr.addMark(marker.from, marker.to, emMark()))
    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(1)

    // ...and removing that mark leaves the marker's text still in one node.
    const marked = harness.actual()[0]
    harness.apply((tr) => tr.removeMark(marked.from, marked.to, schema.marks.em))
    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(1)
    expect(harness.doc.textBetween(harness.actual()[0].from, harness.actual()[0].to)).toBe(
      '[source:12]',
    )
  })

  it('treats a whole-document replacement as a full scan and stays correct', () => {
    const harness = new CitationHarness(docOf('Claim [source:12] here.'))
    const before = harness.ps.stats

    const replacement = docOf('Rewritten [@lyra:77] body.', 'No other markers.')
    // The same whole-document replacement milkdown's `replaceAll` performs.
    harness.apply((tr) => tr.replace(0, tr.doc.content.size, new Slice(replacement.content, 0, 0)))

    expect(harness.ps.stats.fullScans).toBe(before.fullScans + 1)
    expect(harness.ps.stats.incrementalScans).toBe(before.incrementalScans)
    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(1)
    expect(harness.actual()[0].spec.sourceId).toBe(77)
  })

  it('stays correct when a replaced document removes and adds markers', () => {
    const harness = new CitationHarness(docOf('Old [source:1] text.', 'Old [source:2] text.'))
    const replacement = docOf('Fresh [source:3] text.')
    harness.apply((tr) => tr.replace(0, tr.doc.content.size, new Slice(replacement.content, 0, 0)))
    harness.expectMatchesOracle()
    expect(harness.actual()).toHaveLength(1)
    expect(harness.actual()[0].spec.sourceId).toBe(3)
  })
})
