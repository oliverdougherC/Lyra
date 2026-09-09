/**
 * Comment anchors in the editor: each unresolved margin comment's quote, resolved
 * against the live document and decorated with a severity-tinted underline.
 *
 * The resolution mirrors the server's quote anchoring (kuhn's model) with one twist:
 * the server matches against markdown source, but the editor renders a document whose
 * text has shed its markdown syntax. So a quote is stripped of the syntax the renderer
 * strips (heading hashes, emphasis markers, link parentheses), then matched
 * whitespace-normalized against the flattened document text, which carries a map from
 * every character back to its ProseMirror position.
 *
 * Between resolutions the decorations ride ProseMirror's position mapping, so typing
 * above an anchor costs nothing. When an edit deletes a decorated range outright, the
 * whole set re-resolves against the new document - the passage may survive elsewhere,
 * and a quote that no longer matches simply loses its underline (the rail still lists
 * the finding; the server is the authority on orphaning).
 *
 * Within one resolution pass, the normalized document forms are built once and shared
 * by every thread (PLA-512): the whitespace-normalized text always, the canonical
 * case/punctuation form only when some thread misses it. And a flash/unflash never
 * re-resolves at all: the plugin remembers each thread's resolved range and rebuilds
 * the decoration set from those ranges, changing only the one thread's attributes.
 */

import type { Node } from '@milkdown/kit/prose/model'
import { Plugin, PluginKey } from '@milkdown/kit/prose/state'
import type { EditorState, Transaction } from '@milkdown/kit/prose/state'
import { Decoration, DecorationSet } from '@milkdown/kit/prose/view'
import type { EditorView } from '@milkdown/kit/prose/view'
import { $prose } from '@milkdown/kit/utils'

import type { CommentSeverity } from '@/types'

/** What the plugin needs of a thread: identity, the quote to pin, and the tint. */
export interface AnchorThread {
  id: number
  quote: string
  severity: CommentSeverity | null
}

/** Where one thread's quote resolved, in document coordinates. */
export interface ResolvedAnchor {
  id: number
  from: number
  to: number
}

type PluginState = {
  threads: AnchorThread[]
  flashId: number | null
  ranges: ResolvedAnchor[]
  decorations: DecorationSet
  /** How many full quote resolutions have run on this plugin instance. */
  resolutions: number
  /** How many flash-driven rebuilds from stored ranges have run. */
  rebuilds: number
}

type Meta =
  | { type: 'set'; threads: AnchorThread[] }
  | { type: 'flash'; id: number }
  | { type: 'unflash'; id: number }

const key = new PluginKey<PluginState>('lyra-comment-highlights')

/** The raw plugin, exported so tests can drive the production state through a real EditorState. */
export function createCommentPlugin(): Plugin<PluginState> {
  return new Plugin<PluginState>({
    key,
    state: {
      init: () => ({
        threads: [],
        flashId: null,
        ranges: [],
        decorations: DecorationSet.empty,
        resolutions: 0,
        rebuilds: 0,
      }),
      apply(tr: Transaction, state: PluginState): PluginState {
        // The document change is authoritative first: a transaction that edits the text
        // AND carries flash/unflash metadata in the same step must map (or re-resolve)
        // the decorations and ranges before styling, or the flash would paint the
        // pre-edit coordinates.
        let current = state
        if (tr.docChanged) {
          // Keep the stored ranges in step with the decorations: a flash later rebuilds
          // from them, and a stale range would resurrect a pre-edit position.
          const mapping = tr.mapping
          const mappedRanges: ResolvedAnchor[] = []
          for (const range of state.ranges) {
            const from = mapping.map(range.from)
            const to = mapping.map(range.to)
            if (to > from) mappedRanges.push({ ...range, from, to })
          }
          const mapped = state.decorations.map(mapping, tr.doc)
          // A structural edit that swallowed an anchor whole: re-resolve everything,
          // because the passage may have survived somewhere the mapping cannot see.
          if (mappedRanges.length < state.ranges.length) {
            const { ranges, decorations } = resolvePass(tr.doc, state.threads, state.flashId)
            current = {
              threads: state.threads,
              flashId: state.flashId,
              ranges,
              decorations,
              resolutions: state.resolutions + 1,
              rebuilds: state.rebuilds,
            }
          } else {
            current = { ...state, ranges: mappedRanges, decorations: mapped }
          }
        }
        const meta = tr.getMeta(key) as Meta | undefined
        if (meta?.type === 'set') {
          const { ranges, decorations } = resolvePass(tr.doc, meta.threads, current.flashId)
          return {
            threads: meta.threads,
            flashId: current.flashId,
            ranges,
            decorations,
            resolutions: current.resolutions + 1,
            rebuilds: current.rebuilds,
          }
        }
        // The flash lives in the decoration attributes, not in DOM classes: the
        // editor redraws its decorations on scroll, and a class added DOM-side was
        // gone before anyone saw it - verified live. The ranges are already resolved,
        // so a flash rebuilds the set from them and does not re-run a single quote
        // search (PLA-512).
        if (meta?.type === 'flash') {
          return {
            threads: current.threads,
            flashId: meta.id,
            ranges: current.ranges,
            decorations: anchorDecorations(tr.doc, current.threads, current.ranges, meta.id),
            resolutions: current.resolutions,
            rebuilds: current.rebuilds + 1,
          }
        }
        if (meta?.type === 'unflash') {
          if (current.flashId !== meta.id) return current
          return {
            threads: current.threads,
            flashId: null,
            ranges: current.ranges,
            decorations: anchorDecorations(tr.doc, current.threads, current.ranges, null),
            resolutions: current.resolutions,
            rebuilds: current.rebuilds + 1,
          }
        }
        return current
      },
    },
    props: {
      decorations: (state: EditorState) => key.getState(state)?.decorations,
    },
  })
}

export const commentHighlightsPlugin = $prose(() => createCommentPlugin())

/** The plugin key, exported so tests can read the plugin state from a plain EditorState. */
export const commentPluginKey = key

/** Hand the plugin the current unresolved threads; it re-resolves and re-decorates. */
export function setComments(view: EditorView, threads: AnchorThread[]): void {
  view.dispatch(view.state.tr.setMeta(key, { type: 'set', threads } satisfies Meta))
}

/**
 * Scroll the editor to one comment's anchor and flash it. Returns false when the
 * comment has no live anchor to jump to - orphaned, or its quote never resolved.
 */
export function jumpToComment(view: EditorView, commentId: number): boolean {
  // The decoration's own rendered spans are the anchor: if they are not in the DOM,
  // the quote did not resolve and there is nowhere to go.
  if (view.dom.querySelectorAll(`[data-comment-id="${commentId}"]`).length === 0) return false
  view.dispatch(view.state.tr.setMeta(key, { type: 'flash', id: commentId } satisfies Meta))
  // Instant, not smooth: the editor's own DOM churn cancels an in-flight smooth
  // scroll partway there, verified live. Scrolled twice, because the first jump
  // itself triggers a relayout (math and toolbars settle) that shifts the target;
  // the second lands on where it ended up.
  const scroll = () => {
    view.dom.querySelector(`[data-comment-id="${commentId}"]`)?.scrollIntoView({
      block: 'center',
    })
  }
  scroll()
  window.setTimeout(scroll, 150)
  window.setTimeout(() => {
    if (!view.isDestroyed) {
      view.dispatch(view.state.tr.setMeta(key, { type: 'unflash', id: commentId } satisfies Meta))
    }
  }, 2400)
  return true
}

/**
 * Resolve every thread against one shared document index and build the decorations.
 * The per-pass sharing is the whole point of the index (PLA-512): one flatten, one
 * whitespace normalization, at most one canonicalization, no matter how many threads
 * or misses there are.
 */
export function resolvePass(
  doc: Node,
  threads: AnchorThread[],
  flashId: number | null = null,
): { ranges: ResolvedAnchor[]; decorations: DecorationSet } {
  const index = buildDocIndex(flattenDoc(doc))
  const ranges: ResolvedAnchor[] = []
  for (const thread of threads) {
    const range = findQuoteIn(index, thread.quote)
    if (range) ranges.push({ id: thread.id, from: range.from, to: range.to })
  }
  ranges.sort((a, b) => a.from - b.from || a.id - b.id)
  return { ranges, decorations: anchorDecorations(doc, threads, ranges, flashId) }
}

/** The public entry point tests and one-off callers use; the plugin uses `resolvePass`. */
export function resolveAnchors(
  doc: Node,
  threads: AnchorThread[],
  flashId: number | null = null,
): DecorationSet {
  return resolvePass(doc, threads, flashId).decorations
}

/** Build the underline + gutter-marker decorations for the already-resolved ranges. */
function anchorDecorations(
  doc: Node,
  threads: AnchorThread[],
  ranges: ResolvedAnchor[],
  flashId: number | null,
): DecorationSet {
  const byId = new Map<number, AnchorThread>(threads.map((thread) => [thread.id, thread]))
  const decorations: Decoration[] = []
  for (const range of ranges) {
    const thread = byId.get(range.id)
    if (!thread) continue
    const severity = thread.severity ?? 'note'
    const flash = range.id === flashId ? ' comment-anchor--flash' : ''
    decorations.push(
      Decoration.inline(
        range.from,
        range.to,
        {
          'class': `comment-anchor comment-anchor--${severity}${flash}`,
          'data-comment-id': String(thread.id),
          'role': 'button',
          'tabindex': '0',
        },
        { commentId: thread.id },
      ),
      Decoration.widget(
        range.from,
        () => {
          const marker = document.createElement('span')
          marker.className = `comment-gutter-marker comment-gutter-marker--${severity}${flash}`
          marker.dataset.commentId = String(thread.id)
          marker.setAttribute('role', 'button')
          marker.setAttribute('tabindex', '0')
          marker.setAttribute('aria-label', `Open comment ${thread.id}`)
          marker.textContent = '•'
          return marker
        },
        { side: -1, gutterCommentId: thread.id },
      ),
    )
  }
  return DecorationSet.create(doc, decorations)
}

/** The document as one string, with `positions[i]` = the doc position of `text[i]`. */
export interface FlatDoc {
  text: string
  positions: number[]
}

export function flattenDoc(doc: Node): FlatDoc {
  let text = ''
  const positions: number[] = []
  doc.descendants((node, pos) => {
    if (node.isText && node.text) {
      for (let index = 0; index < node.text.length; index++) {
        text += node.text[index]
        positions.push(pos + index)
      }
      return true
    }
    // A block boundary reads as a newline, pinned to the boundary itself, so a quote
    // spanning two paragraphs still resolves and still maps back to real positions.
    if (node.isBlock && text.length > 0 && !text.endsWith('\n')) {
      text += '\n'
      positions.push(pos)
    }
    return true
  })
  return { text, positions }
}

type Normalized = { norm: string; map: number[] }

/**
 * The per-pass shared index over one document snapshot: the flattened text plus lazily
 * built normalized forms. `space()` collapses whitespace (the primary match);
 * `canonical()` folds case/punctuation and memoizes the word list for fuzzy matching,
 * built only when some thread misses the primary form. `builds` counts how often each
 * form was actually constructed, so a test can prove the sharing: exactly one space
 * build and at most one canonical build per pass, no matter how many threads.
 */
export interface DocIndex {
  flat: FlatDoc
  space: () => Normalized
  canonical: () => Normalized & { words: Word[] }
  builds: { space: number; canonical: number }
}

export function buildDocIndex(flat: FlatDoc): DocIndex {
  let space: Normalized | null = null
  let canon: (Normalized & { words: Word[] }) | null = null
  const builds = { space: 0, canonical: 0 }
  return {
    flat,
    builds,
    space: () => {
      if (!space) {
        space = normalizeWithMap(flat.text)
        builds.space += 1
      }
      return space
    },
    canonical: () => {
      if (!canon) {
        const base = canonicalWithMap(flat.text)
        canon = { ...base, words: wordsIn(base.norm) }
        builds.canonical += 1
      }
      return canon
    },
  }
}

/**
 * A markdown quote, reduced to what the rendered document actually shows: heading
 * hashes, emphasis and code markers, list bullets, blockquote angles, link targets,
 * and backslash escapes all gone. Lossy on purpose - an asterisk of real prose lost
 * here costs one match, and the rail still lists the finding.
 */
export function stripMarkdownQuote(quote: string): string {
  return quote
    .replace(/\\([[\]()*_#`>-])/g, '$1') // unescape first, so \[TODO reads as [TODO
    .replace(/^[ \t]*#{1,6}[ \t]+/gm, '') // heading hashes
    .replace(/^[ \t]*(?:[-*+]|\d+[.)])[ \t]+/gm, '') // list markers
    .replace(/^[ \t]*>[ \t]?/gm, '') // blockquote angles
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1') // links keep their text
    .replace(/\*\*|__|[*`]/g, '') // emphasis and code markers
}

/** Where the quote sits in the document, as ProseMirror positions, or null. */
export function findQuote(flat: FlatDoc, quote: string): { from: number; to: number } | null {
  return findQuoteIn(buildDocIndex(flat), quote)
}

/** The same lookup against a shared pass index, so one pass normalizes once. */
export function findQuoteIn(index: DocIndex, quote: string): { from: number; to: number } | null {
  const stripped = stripMarkdownQuote(quote)
  const target = normalizeSpace(stripped)
  if (!target || index.flat.text.length === 0) return null
  const space = index.space()
  let found = space.norm.indexOf(target)
  let endIndex = found === -1 ? -1 : found + target.length - 1
  let normalizedMap = space.map

  // Match the server's punctuation/case normalization before its conservative fuzzy
  // fallback. A model that copied curly quotes as straight quotes should not create a
  // server-side anchor that disappears in the editor.
  if (found === -1) {
    const canon = index.canonical()
    const canonicalTarget = canonical(stripped)
    if (!canonicalTarget) return null
    found = canon.norm.indexOf(canonicalTarget)
    endIndex = found === -1 ? -1 : found + canonicalTarget.length - 1
    normalizedMap = canon.map
    if (found === -1) {
      const fuzzy = fuzzySubstring(canon.norm, canonicalTarget, canon.words)
      if (!fuzzy) return null
      found = fuzzy.from
      endIndex = fuzzy.to - 1
    }
  }

  const startFlat = normalizedMap[found]
  const endFlat = normalizedMap[endIndex]
  return { from: index.flat.positions[startFlat], to: index.flat.positions[endFlat] + 1 }
}

function normalizeSpace(value: string): string {
  return value.replace(/\s+/g, ' ').trim()
}

/** Whitespace runs collapsed to single spaces; `map[i]` = raw offset of `norm[i]`. */
function normalizeWithMap(content: string): Normalized {
  let norm = ''
  const map: number[] = []
  let inSpace = false
  for (let index = 0; index < content.length; index++) {
    const char = content[index]
    if (/\s/.test(char)) {
      inSpace = true
      continue
    }
    if (inSpace && norm.length > 0) {
      norm += ' '
      map.push(index - 1)
    }
    inSpace = false
    norm += char
    map.push(index)
  }
  return { norm, map }
}

const PUNCTUATION: Record<string, string> = {
  '\u2018': "'",
  '\u2019': "'",
  '\u201a': "'",
  '\u201b': "'",
  '\u201c': '"',
  '\u201d': '"',
  '\u201e': '"',
  '\u201f': '"',
  '\u2010': '-',
  '\u2011': '-',
  '\u2012': '-',
  '\u2013': '-',
  '\u2014': '-',
  '\u2015': '-',
  '\u2026': '...',
}

function canonical(value: string): string {
  return canonicalWithMap(value).norm.trim()
}

function canonicalWithMap(content: string): Normalized {
  let norm = ''
  const map: number[] = []
  let inSpace = false
  for (let index = 0; index < content.length; index++) {
    const folded = content[index].normalize('NFKC').toLocaleLowerCase()
    for (const input of folded) {
      const output = PUNCTUATION[input] ?? input
      for (const char of output) {
        if (/\s/.test(char)) {
          inSpace = true
          continue
        }
        if (inSpace && norm.length > 0) {
          norm += ' '
          map.push(Math.max(0, index - 1))
        }
        inSpace = false
        norm += char
        map.push(index)
      }
    }
  }
  return { norm, map }
}

type Word = { value: string; from: number; to: number }

/**
 * The same conservative shape as the server: enough words, token coverage, and
 * similarity. The word list is memoized in the pass index, so a pass with many misses
 * tokenizes the document once.
 */
function fuzzySubstring(
  content: string,
  target: string,
  words?: Word[],
): { from: number; to: number } | null {
  const corpus = words ?? wordsIn(content)
  const targetWords = wordsIn(target)
  if (targetWords.length < 4 || target.length < 20 || corpus.length === 0) return null
  const targetSet = new Set(targetWords.map((word) => word.value))
  const spread = Math.max(1, Math.round(targetWords.length * 0.15))
  const candidates: Array<{ score: number; from: number; to: number }> = []
  for (
    let size = Math.max(1, targetWords.length - spread);
    size <= targetWords.length + spread;
    size++
  ) {
    for (let index = 0; index + size <= corpus.length; index++) {
      const window = corpus.slice(index, index + size)
      const covered = new Set(
        window.map((word) => word.value).filter((word) => targetSet.has(word)),
      )
      if (covered.size / targetSet.size < 0.75) continue
      const from = window[0].from
      const to = window[window.length - 1].to
      const score = similarity(target, content.slice(from, to))
      if (score >= 0.82) candidates.push({ score, from, to })
    }
  }
  candidates.sort((a, b) => b.score - a.score || a.from - b.from)
  if (candidates.length === 0) return null
  if (
    candidates.length > 1 &&
    candidates[0].score - candidates[1].score < 0.025 &&
    (candidates[0].from !== candidates[1].from || candidates[0].to !== candidates[1].to)
  ) {
    return null
  }
  return candidates[0]
}

function wordsIn(value: string): Word[] {
  return [...value.matchAll(/[\p{L}\p{N}_]+(?:['-][\p{L}\p{N}_]+)*/gu)].map((match) => ({
    value: match[0],
    from: match.index,
    to: match.index + match[0].length,
  }))
}

function similarity(left: string, right: string): number {
  if (left === right) return 1
  if (!left || !right) return 0
  let previous = Array.from({ length: right.length + 1 }, (_, index) => index)
  for (let row = 1; row <= left.length; row++) {
    const current = [row]
    for (let column = 1; column <= right.length; column++) {
      current[column] = Math.min(
        current[column - 1] + 1,
        previous[column] + 1,
        previous[column - 1] + (left[row - 1] === right[column - 1] ? 0 : 1),
      )
    }
    previous = current
  }
  return 1 - previous[right.length] / Math.max(left.length, right.length)
}
