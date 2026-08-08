/**
 * Math delimiters in a draft body, normalized to the ones the editor actually parses.
 *
 * The mirror of `backend/core/mathnorm.py`, and deliberately a separate thing from
 * `chat/markdown-utils.ts`: that one tokenizes for rendering and returns render tokens,
 * while a document body has to come out the other side as markdown the editor can parse
 * and the student can keep editing. Same conversions, different output.
 *
 * New AI text is normalized server-side where it lands, so this is for bodies written
 * before that existed - and for the `/write` widget's passage, which is accepted in the
 * client. Milkdown's remark-math only understands `$` and `$$`, so a body holding
 * `\(x\)` renders as literal backslashes in the editor while the very same text renders
 * correctly in the chat pane beside it.
 *
 * Two things are never touched, for the same reasons as the backend copy: `\[TODO:` is
 * the editor's own escaping of a section's intent marker and the section index depends
 * on it, and `\$` is a dollar sign somebody wanted.
 */

const DISPLAY_ENVIRONMENTS = [
  'equation',
  'align',
  'gather',
  'multline',
  'cases',
  'dcases',
  'aligned',
  'split',
  'matrix',
  'pmatrix',
  'bmatrix',
]

const ENVIRONMENT_NAMES = DISPLAY_ENVIRONMENTS.map((name) => `${name}\\*?`).join('|')

/**
 * A bare environment standing alone on its own lines. Only reached for text outside an
 * existing `$$` block - see `splitVerbatim`, without which this wraps an already
 * delimited environment again on every load.
 */
const BARE_ENVIRONMENT = new RegExp(
  `(?<!\\\\)^([ \\t]*)(\\\\begin\\{(?:${ENVIRONMENT_NAMES})\\}[\\s\\S]*?\\\\end\\{(?:${ENVIRONMENT_NAMES})\\})[ \\t]*$`,
  'gm',
)

/** `\(...\)`, non-greedy so two spans on one line stay two spans. */
const INLINE_PAREN = /(?<!\\)\\\(([\s\S]+?)(?<!\\)\\\)/g

/**
 * `\[...\]`. The TODO guard is a lookahead rather than a filter afterwards: a greedy
 * reading of `\[TODO: cite]` would run to the end of the document hunting a `\]`.
 */
const DISPLAY_BRACKET = /(?<!\\)\\\[(?!\s*TODO\b)([\s\S]+?)(?<!\\)\\\]/g

const FENCE = /^ {0,3}(`{3,}|~{3,})/

/**
 * Rewrite LaTeX-style math delimiters to the `$` forms the editor reads.
 *
 * Idempotent: already-normalized text comes back unchanged, which matters because a body
 * is normalized on load and again whenever the server sends a fresh one.
 */
export function normalizeMathDelimiters(text: string): string {
  if (!text || !text.includes('\\')) return text
  return splitVerbatim(text)
    .map(({ content, verbatim }) => (verbatim ? content : normalizeProse(content)))
    .join('')
}

function normalizeProse(text: string): string {
  return text
    .replace(INLINE_PAREN, (_match, inner: string) => `$${inner.trim()}$`)
    .replace(DISPLAY_BRACKET, (_match, inner: string) => `\n$$\n${inner.trim()}\n$$\n`)
    .replace(
      BARE_ENVIRONMENT,
      (_match, indent: string, block: string) => `${indent}$$\n${block}\n$$`,
    )
}

/**
 * The text as runs, each flagged "leave this alone".
 *
 * Two kinds are left alone. A fenced code block is verbatim: a snippet showing `\(` is
 * demonstrating the delimiter, not using it. A `$$` block is already display math, and
 * running the environment rule inside one wraps it again - which, because bodies are
 * normalized on every load, nests one more `$$` pair around the same equation each time
 * the draft is opened.
 */
function splitVerbatim(text: string): { content: string; verbatim: boolean }[] {
  const runs: { content: string; verbatim: boolean }[] = []
  let buffer: string[] = []
  let marker: string | null = null
  let inMath = false

  for (const line of text.split(/(?<=\n)/)) {
    const fence = FENCE.exec(line)
    const isMathDelimiter = marker === null && line.trim() === '$$'
    if (isMathDelimiter) {
      if (inMath) {
        buffer.push(line)
        runs.push({ content: buffer.join(''), verbatim: true })
        buffer = []
        inMath = false
      } else {
        runs.push({ content: buffer.join(''), verbatim: false })
        buffer = [line]
        inMath = true
      }
    } else if (inMath) {
      buffer.push(line)
    } else if (marker === null && fence) {
      runs.push({ content: buffer.join(''), verbatim: false })
      buffer = [line]
      marker = fence[1]
    } else if (marker !== null && fence && fence[1][0] === marker[0]) {
      buffer.push(line)
      runs.push({ content: buffer.join(''), verbatim: true })
      buffer = []
      marker = null
    } else {
      buffer.push(line)
    }
  }
  if (buffer.length) runs.push({ content: buffer.join(''), verbatim: marker !== null || inMath })
  return runs
}
