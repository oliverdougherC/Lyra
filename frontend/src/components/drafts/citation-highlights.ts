import type { Node } from '@milkdown/kit/prose/model'
import type { Transaction } from '@milkdown/kit/prose/state'
import { Plugin, PluginKey } from '@milkdown/kit/prose/state'
import { AddMarkStep, RemoveMarkStep, ReplaceStep } from '@milkdown/kit/prose/transform'
import { Decoration, DecorationSet } from '@milkdown/kit/prose/view'
import { $prose } from '@milkdown/kit/utils'

// The writer-facing syntax is `[source:12]`; the older export syntax remains readable
// while stored drafts converge, so neither form turns back into raw punctuation.
const CITATION = /\[(?:source:|@lyra(?::|-))(\d+)\]/g
const key = new PluginKey<CitationPluginState>('lyra-draft-citations')

/**
 * What a decoration-scan cost, tracked so production plugin tests can measure the work a
 * transaction actually did (PLA-512) instead of asserting it:
 *
 * - `fullScans` / `incrementalScans` count which path ran,
 * - `nodesVisited` / `charsScanned` accumulate the text nodes and characters a scan read.
 *
 * An idle editor (selection moves, no document change) performs no scans at all.
 */
export interface CitationScanStats {
  fullScans: number
  incrementalScans: number
  nodesVisited: number
  charsScanned: number
}

export interface CitationPluginState {
  decorations: DecorationSet
  stats: CitationScanStats
}

function citationAt(match: RegExpMatchArray, nodePos: number): Decoration {
  const sourceId = Number(match[1])
  const index = match.index ?? 0
  return Decoration.inline(
    nodePos + index,
    nodePos + index + match[0].length,
    {
      'class': 'draft-citation',
      'data-source-id': String(sourceId),
      'title': `Open source ${sourceId}`,
      'role': 'button',
      'tabindex': '0',
    },
    { sourceId },
  )
}

/**
 * Scan `[from, to)` of the live document for citation markers, visiting only the text
 * nodes inside the window. A marker that touches the window's edge but is not fully
 * inside it is left to the window (or the mapped survivor) that owns it, so overlapping
 * windows never produce a duplicate decoration.
 */
function scanRange(doc: Node, from: number, to: number, stats: CitationScanStats): Decoration[] {
  if (to <= from) return []
  const found: Decoration[] = []
  doc.nodesBetween(from, to, (node, pos) => {
    if (!node.isText || !node.text) return true
    stats.nodesVisited += 1
    stats.charsScanned += node.text.length
    for (const match of node.text.matchAll(CITATION)) {
      const start = pos + (match.index ?? 0)
      if (start < from || start + match[0].length > to) continue
      const sourceId = Number(match[1])
      if (!Number.isSafeInteger(sourceId) || sourceId < 1) continue
      found.push(citationAt(match, pos))
    }
    return true
  })
  return found
}

/**
 * The full-document scan: every text node, every marker. Used at editor init, when the
 * document is replaced wholesale (a server rewrite), and as the always-correct fallback.
 */
export function citationDecorations(doc: Node): DecorationSet {
  const stats: CitationScanStats = {
    fullScans: 0,
    incrementalScans: 0,
    nodesVisited: 0,
    charsScanned: 0,
  }
  return DecorationSet.create(doc, scanRange(doc, 0, doc.content.size, stats))
}

/** Map unchanged decorations and rescan complete affected textblocks. */
function mapOrRescan(tr: Transaction, old: CitationPluginState): CitationPluginState {
  const stats = { ...old.stats }
  const doc = tr.doc
  const fullScan = (): CitationPluginState => {
    stats.fullScans += 1
    return {
      decorations: DecorationSet.create(doc, scanRange(doc, 0, doc.content.size, stats)),
      stats,
    }
  }
  const first = tr.steps[0]
  if (first instanceof ReplaceStep && first.from === 0 && first.to >= tr.before.content.size)
    return fullScan()

  const changes: Array<[number, number]> = []
  for (let index = 0; index < tr.steps.length; index += 1) {
    const step = tr.steps[index]
    const remaining = tr.mapping.slice(index + 1)
    let hasRange = false
    tr.mapping.maps[index].forEach((_oldFrom, _oldTo, from, to) => {
      hasRange = true
      changes.push([remaining.map(from, -1), remaining.map(to, 1)])
    })
    // Mark steps have empty position maps but can split or join text nodes, changing
    // whether the per-node citation syntax matches. Unknown steps use the full oracle.
    if (!hasRange) {
      if (step instanceof AddMarkStep || step instanceof RemoveMarkStep) {
        changes.push([remaining.map(step.from, -1), remaining.map(step.to, 1)])
      } else return fullScan()
    }
  }

  const blocks = new Map<number, number>()
  for (const [from, to] of changes) {
    // Include both sides of a deletion or structural boundary. A whole textblock
    // provides context for a marker completed across the changed range or a mark.
    const start = Math.max(0, Math.min(from, to) - 1)
    const end = Math.min(doc.content.size, Math.max(from, to) + 1)
    if (start > end) return fullScan()
    doc.nodesBetween(start, end, (node, pos) => {
      if (!node.isTextblock) return true
      blocks.set(pos, pos + node.nodeSize)
      return false
    })
  }
  if (blocks.size === 0) return fullScan()

  let decorations = old.decorations.map(tr.mapping, doc)
  const additions: Decoration[] = []
  for (const [from, to] of blocks) {
    decorations = decorations.remove(decorations.find(from, to))
    additions.push(...scanRange(doc, from, to, stats))
  }
  additions.sort((left, right) => left.from - right.from || left.to - right.to)
  stats.incrementalScans += 1
  return { decorations: decorations.add(doc, additions), stats }
}

/** The raw plugin, exported so tests can drive the production state through a real EditorState. */
export function createCitationPlugin(): Plugin<CitationPluginState> {
  return new Plugin<CitationPluginState>({
    key,
    state: {
      init: (_config, state) => {
        const stats: CitationScanStats = {
          fullScans: 1,
          incrementalScans: 0,
          nodesVisited: 0,
          charsScanned: 0,
        }
        return {
          decorations: DecorationSet.create(
            state.doc,
            scanRange(state.doc, 0, state.doc.content.size, stats),
          ),
          stats,
        }
      },
      apply: (transaction, current) => {
        if (!transaction.docChanged) return current
        return mapOrRescan(transaction, current)
      },
    },
    props: {
      decorations: (state) => key.getState(state)?.decorations,
    },
  })
}

export const citationHighlightsPlugin = $prose(() => createCitationPlugin())

/** The plugin key, exported so tests can read the plugin state from a plain EditorState. */
export const citationPluginKey = key
