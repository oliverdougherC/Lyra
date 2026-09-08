/**
 * What Lyra does to model markdown before handing it to a renderer.
 *
 * The rules here are the contracts from docs/ui-phase-1.md: display math sits on its own
 * blank-line-separated rows, `$...$` is reserved for short inline quantities, and code is
 * never rewritten. A streamed answer arrives a token at a time, and these rules have to hold
 * on every partial buffer or the reader sees an equation change its mind. The policies:
 *
 * - **Deliberation is line-local.** Undelimited mathematics is repaired line by line, and a
 *   line gets its interpretation the moment the line has fully arrived. The settled render
 *   applies the same per-line rule, so completing a turn never switches on a new
 *   answer-wide interpretation under the reader's eyes.
 * - **Explicit inline math is always inline.** A `$...$` or `\(...\)` span the model wrote
 *   closed stays inline at every later chunk and at the terminal handoff: a newline after
 *   it, or the stream finishing, never re-sorts it into a display block. Display equations
 *   are the ones the model delimited as display (`$$`, `\[...\]`, an environment), and
 *   they are what the lifting rule applies to — moved onto their own rows they are indented
 *   (and, inside a blockquote, marker-prefixed) to the content column of their line, so
 *   lists and quotes keep their structure.
 * - **A lone dollar is a currency, not a ghost.** An unescaped `$` never opens a span when
 *   a space follows it (the parser's own rule), and a `$` followed by a digit is a price: it
 *   is escaped in the render copy, because otherwise the parser pairs it with the next
 *   price's dollar and typesets the prose between them as an equation. A settled line that
 *   closed without a valid closer is left literal, and an unclosed equation at the very
 *   tail of an open line is withheld — only that line.
 */

const DISPLAY_ENVIRONMENTS = [
  'equation',
  'equation*',
  'align',
  'align*',
  'gather',
  'gather*',
  'multline',
  'multline*',
  'cases',
  'dcases',
  'aligned',
  'split',
  'matrix',
  'pmatrix',
  'bmatrix',
] as const

const DISPLAY_ENVIRONMENT_PATTERN = new RegExp(
  `^\\\\begin\\{(${DISPLAY_ENVIRONMENTS.join('|').replaceAll('*', '\\*')})\\}`,
)

/** The block a display equation sits in: the blockquote run, and where content starts. */
type Container = {
  /** The blockquote markers at the start of the line, e.g. `> ` or `> > `. */
  quotePrefix: string
  /** The column in the line where the block's content begins, after any list marker. */
  contentCol: number
}

type RenderToken = {
  text: string
  display?: boolean
  /** The block a lifted equation stays in; plain text has none. */
  container?: Container
}

function isEscaped(source: string, index: number): boolean {
  let slashes = 0
  for (let cursor = index - 1; cursor >= 0 && source[cursor] === '\\'; cursor -= 1) {
    slashes += 1
  }
  return slashes % 2 === 1
}

/**
 * The column at which a fence may start on a line: past the block's own markers, the way a
 * content column is found. A quote's `>` run (each optionally followed by a space) and a
 * list item's marker both precede the fence that opens its code block, and they precede
 * the line that closes it just the same.
 */
function fenceContentCol(source: string, lineStart: number): number {
  let cursor = lineStart
  let indent = 0
  while (indent < 3 && source[cursor] === ' ') {
    cursor += 1
    indent += 1
  }
  while (source[cursor] === '>') {
    cursor += 1
    if (source[cursor] === ' ') cursor += 1
  }
  while (source[cursor] === ' ') cursor += 1
  const marker = LIST_MARKER_PATTERN.exec(source.slice(cursor))
  if (marker) {
    cursor += marker[0].length
    while (source[cursor] === ' ') cursor += 1
  }
  return cursor
}

function lineStartsFence(
  source: string,
  index: number,
): { end: number; char: '`' | '~'; length: number } | null {
  if (index > 0 && source[index - 1] !== '\n') return null

  const cursor = fenceContentCol(source, index)
  const char = source[cursor]
  if (char !== '`' && char !== '~') return null

  let length = 0
  while (source[cursor + length] === char) length += 1
  if (length < 3) return null
  return { end: cursor + length, char, length }
}

function findFenceEnd(
  source: string,
  openingEnd: number,
  char: '`' | '~',
  length: number,
): number | null {
  let lineStart = source.indexOf('\n', openingEnd)
  if (lineStart === -1) return null
  lineStart += 1

  while (lineStart < source.length) {
    const cursor = fenceContentCol(source, lineStart)

    let run = 0
    while (source[cursor + run] === char) run += 1
    if (
      run >= length &&
      !source
        .slice(cursor + run)
        .split('\n', 1)[0]
        .trim()
    ) {
      const newline = source.indexOf('\n', cursor + run)
      return newline === -1 ? source.length : newline + 1
    }

    const nextLine = source.indexOf('\n', lineStart)
    if (nextLine === -1) break
    lineStart = nextLine + 1
  }
  return null
}

function findClosing(source: string, start: number, closer: string): number | null {
  let cursor = start
  while (cursor < source.length) {
    const found = source.indexOf(closer, cursor)
    if (found === -1) return null
    if (!isEscaped(source, found)) return found
    cursor = found + 1
  }
  return null
}

/**
 * The closer of a single-`$` span, honoring the parser's delimiter rules: the closing
 * dollar must not be escaped, must not be preceded by a space, and cannot cross the end
 * of the line.
 */
function findDollarClosing(source: string, start: number, end: number): number | null {
  let cursor = start
  while (cursor < end) {
    const found = source.indexOf('$', cursor)
    if (found === -1 || found >= end) return null
    if (!isEscaped(source, found) && source[found - 1] !== ' ') return found
    cursor = found + 1
  }
  return null
}

/** Whether another unescaped `$` sits in `[from, to)` — the one a parser would pair against. */
function hasUnescapedDollar(source: string, from: number, to: number): boolean {
  for (let at = source.indexOf('$', from); at !== -1 && at < to; at = source.indexOf('$', at + 1)) {
    if (!isEscaped(source, at)) return true
  }
  return false
}

/** A list marker: `- ` `* ` `+ `, `1. `, `1) `, or `(1) `, possibly at the end of the line. */
const LIST_MARKER_PATTERN = /^(?:[+*-]|\(\d{1,9}\)|\d{1,9}[.)])(?: |$)/

/** The block structure of the text that precedes an equation on its line. */
function parseLineContainer(line: string): Container {
  let j = 0
  while (j < 3 && line[j] === ' ') j += 1
  let quotePrefix = line.slice(0, j)
  while (line[j] === '>') {
    quotePrefix += '>'
    j += 1
    if (line[j] === ' ') {
      quotePrefix += ' '
      j += 1
    }
  }
  let s = j
  while (line[s] === ' ') s += 1
  const marker = LIST_MARKER_PATTERN.exec(line.slice(s))
  if (!marker) return { quotePrefix, contentCol: s }
  return { quotePrefix, contentCol: s + marker[0].length }
}

function containerAt(source: string, spanStart: number): Container {
  const lineStart = source.lastIndexOf('\n', spanStart - 1) + 1
  return parseLineContainer(source.slice(lineStart, spanStart))
}

/**
 * The line at its content column: the leading indent and blockquote markers stripped.
 *
 * A display block lifted into a blockquote or a list carries those markers on every row
 * (`>   $$`, `  $$`), and a detector that only trims whitespace misses them — which would
 * leave the block's rows looking like bare math and get them re-wrapped on the next pass.
 * The render copy must be a fixed point, so the delimiters are read where they sit. The
 * trailing whitespace is dropped too, so the result behaves like the `trim()` it replaces.
 */
function atContentCol(line: string): string {
  let j = 0
  while (j < 3 && line[j] === ' ') j += 1
  while (line[j] === '>') {
    j += 1
    if (line[j] === ' ') j += 1
  }
  while (line[j] === ' ') j += 1
  return line.slice(j).trimEnd()
}

/** Whether a line closes the code fence currently open on it. */
function isFenceClosing(line: string, char: '`' | '~', length: number): boolean {
  const fence = lineStartsFence(line, 0)
  return (
    fence !== null &&
    fence.char === char &&
    fence.length >= length &&
    line.slice(fence.end).trim() === ''
  )
}

/**
 * An equation lifted onto its own rows, kept inside the block its line belongs to.
 * Every row carries the block's continuation prefix, so a list item stays a list item and
 * a blockquote stays a blockquote.
 */
function formatDisplayMath(content: string, container: Container): string {
  const pad =
    container.quotePrefix +
    ' '.repeat(Math.max(0, container.contentCol - container.quotePrefix.length))
  // A multi-line block read back from the source already carries the block's continuation
  // prefix on its rows (`>   \frac{1}{2}`); a bare `trim()` cannot drop it. Strip one
  // prefix per row and re-apply the pad, so the block re-pads itself instead of doubling.
  const stripPad = (line: string) =>
    pad !== '' && line.startsWith(pad) ? line.slice(pad.length) : line
  const inner = content.split('\n').map(stripPad).join('\n').trim()
  return [`$$`, ...inner.split('\n'), '$$']
    .map((line) => (pad !== '' ? `${pad}${line}` : line))
    .join('\n')
}

/** LaTeX commands common enough in an answer that seeing one means mathematics. */
const MATH_COMMAND =
  /\\(?:d?frac|int|iint|oint|sum|prod|sqrt|left|right|cdot|times|div|infty|partial|nabla|lim|log|ln|sin|cos|tan|sec|csc|cot|sinh|cosh|tanh|exp|alpha|beta|gamma|delta|epsilon|zeta|eta|theta|kappa|lambda|mu|nu|xi|pi|rho|sigma|tau|phi|chi|psi|omega|Gamma|Delta|Theta|Lambda|Sigma|Phi|Psi|Omega|neq|leq|geq|ll|gg|approx|equiv|propto|pm|mp|to|rightarrow|Rightarrow|text|mathrm|mathbb|hat|bar|tilde|vec|overline|underline|langle|rangle|lfloor|rfloor|quad|qquad)\b/

/** A leading enumeration label: `(a)`, `a)`, `1.`, `(iii)`. Kept out of the math. */
const LEADING_LABEL = /^(\s*\(?[0-9a-z]{1,4}[).]\s*)/i

/**
 * Wrap mathematics a model wrote without delimiters.
 *
 * `"answer": "(a) x(t) = \\frac{1}{2\\pi(2-jt)}"` is a real reply, and without `$` around
 * it the student is shown the characters the model typed instead of a fraction. The prompt
 * asks for the delimiters and mostly gets them; this is what happens the rest of the time.
 *
 * The repair is line-local, and a line keeps its interpretation for the life of the answer:
 * each line gets its dollars the moment it has fully arrived, so a streamed answer does not
 * sit as literal TeX for a whole turn and then jump to typeset at finalization. A line is
 * never repaired while it is still arriving (`completeLinesOnly`), because a half-arrived
 * fraction would be wrapped and typeset with one arm; it is repaired on the chunk in which
 * the line completes, and it is the same line either way.
 *
 * Every guard here exists to keep the repair away from text it would damage. A line inside
 * a code fence, a `$$` block, or an environment is not prose to repair; a line that already
 * carries delimiters, code, or a display delimiter is left alone. Within that, only lines
 * carrying a known LaTeX command are touched.
 */
export function repairUndelimitedMath(
  source: string,
  { completeLinesOnly = false }: { completeLinesOnly?: boolean } = {},
): string {
  const lines = source.split('\n')
  // The last line is complete only when the source itself ends with its newline.
  const completeThrough =
    completeLinesOnly && !source.endsWith('\n') ? lines.length - 1 : lines.length
  let inDisplay = false
  let inFence: { char: '`' | '~'; length: number } | null = null
  let inEnv: string | null = null

  return lines
    .map((line, index) => {
      // Read the delimiters at the content column: a lifted display block carries the
      // blockquote and list markers on every row, and the block's rows are math the
      // wrapper below must not touch.
      const content = atContentCol(line)
      if (inDisplay) {
        if (content.endsWith('$$')) inDisplay = false
        return line
      }
      if (content.startsWith('$$')) {
        // `$$x$$` on a row of its own is a complete display; an opening `$$` (or `$$x`)
        // holds the rows after it until their closing `$$`.
        if (content.length === 2 || !content.endsWith('$$')) inDisplay = true
        return line
      }
      if (inFence) {
        if (isFenceClosing(line, inFence.char, inFence.length)) inFence = null
        return line
      }
      const fence = lineStartsFence(line, 0)
      if (fence) {
        inFence = fence
        return line
      }
      if (inEnv) {
        // Interior lines belong to the environment until its closing arrives; none of
        // them is prose to repair.
        if (content.includes(`\\end{${inEnv}}`)) inEnv = null
        return line
      }
      const env = /\\begin\{([A-Za-z*]+)\}/.exec(content)
      if (env) {
        // An environment already delimits itself, so it is left exactly as written. The
        // environment stays open across lines until its closing arrives; a line that
        // closes it on the spot leaves nothing open behind.
        inEnv = content.slice(env[0].length).includes(`\\end{${env[1]}}`) ? null : env[1]
        return line
      }
      if (index >= completeThrough) return line
      if (
        line.includes('$') ||
        line.includes('`') ||
        line.includes('\\(') ||
        line.includes('\\[')
      ) {
        return line
      }
      if (!MATH_COMMAND.test(line)) return line
      const prefix = LEADING_LABEL.exec(line)?.[1] ?? ''
      const rest = line.slice(prefix.length).trim()
      // The repaired line is set as a display block: the whole line is the equation, and
      // an equation that owns its line is centred, not stranded mid-sentence. The `$$`
      // delimiters are what the tokenizer lifts into the line's container.
      return rest ? `${prefix}$$${rest}$$` : line
    })
    .join('\n')
}

/**
 * A whitespace-separated token that is mathematics rather than a word. Three signs count,
 * and each is something prose does not do: a superscript or subscript, a LaTeX command, or
 * a single-letter function applied to an argument, which is `u(t)`, `x(t-1)`, `X(jw)`.
 *
 * The single-letter rule is what keeps `(a)` out. An enumeration label is a bracket with no
 * function in front of it, so it stays the label it is instead of being typeset as a
 * variable in the middle of a heading.
 */
const MATH_TOKEN = /[\^_](\{|[A-Za-z0-9])|\\[A-Za-z]+|(?:^|[^A-Za-z])[A-Za-z]\(/

/** Trailing punctuation belongs to the sentence, not to the mathematics it follows. */
const TRAILING_PUNCTUATION = /[.,;:!?]+$/

/**
 * A token that is nothing but an operator. Alone it says nothing, but between two
 * mathematical tokens it joins them into one expression, so `h(t) = e^{t}u(-t)` is set as
 * one equation rather than as two spans with a full-size prose `=` stranded between them.
 */
const MATH_OPERATOR = /^[=+\-*/<>|]+$/

/**
 * Wrap the mathematical spans of a short label, leaving its words alone.
 *
 * `repairUndelimitedMath` wraps a whole line, which is right for an answer that is nothing
 * but an equation and wrong for a step title. "Part (a) Convolution of u(t) and e^{-t}u(t)"
 * is a real title, and wrapping all of it would typeset "Part", "Convolution", "of" and
 * "and" as strings of italic variables. So this wraps the runs of mathematics and nothing
 * else, giving "Part (a) Convolution of $u(t)$ and $e^{-t}u(t)$".
 *
 * Skipped entirely the moment the label carries a `$` or a backtick of its own: a label
 * that delimited its own mathematics is already right, and a second pass over it could only
 * make it wrong.
 */
export function repairLabelMath(label: string): string {
  if (label.includes('$') || label.includes('`')) return label

  const pieces = label.split(/(\s+)/).filter(Boolean)
  const space = pieces.map((piece) => /^\s+$/.test(piece))
  const math = pieces.map((piece, index) => !space[index] && MATH_TOKEN.test(piece))
  if (!math.some(Boolean)) return label

  // A run reaches from one mathematical token to the next across the spaces and bare
  // operators between them, and stops at the first word. Anything else splits an equation
  // at its own equals sign.
  const inRun = [...math]
  let previous = -1
  for (let index = 0; index < pieces.length; index += 1) {
    if (!math[index]) continue
    const between = pieces.slice(previous + 1, index)
    if (
      previous >= 0 &&
      between.every((piece) => /^\s+$/.test(piece) || MATH_OPERATOR.test(piece))
    ) {
      for (let filled = previous + 1; filled < index; filled += 1) inRun[filled] = true
    }
    previous = index
  }

  const out: string[] = []
  let run: string[] = []

  const flushRun = () => {
    if (run.length === 0) return
    // Punctuation that ended the run belongs to the sentence, so a title closing on a full
    // stop does not typeset the full stop as part of the mathematics.
    const joined = run.join('')
    const tail = TRAILING_PUNCTUATION.exec(joined)?.[0] ?? ''
    out.push(`$${tail ? joined.slice(0, -tail.length) : joined}$${tail}`)
    run = []
  }

  pieces.forEach((piece, index) => {
    if (inRun[index]) {
      run.push(piece)
      return
    }
    flushRun()
    out.push(piece)
  })
  flushRun()

  return out.join('')
}

function addToken(
  tokens: RenderToken[],
  text: string,
  display = false,
  container?: Container,
): void {
  if (!text) return
  tokens.push({
    text,
    ...(display ? { display: true, ...(container ? { container } : {}) } : {}),
  })
}

/**
 * Sentence-ending punctuation stranded after a display equation, optionally across one line
 * break. A blank line is deliberately not matched: that is a new paragraph the author meant
 * to start, not a full stop that fell off the end of an equation.
 */
const STRANDED_PUNCTUATION = /^[ \t]*\n?[ \t]*([.,;:!?]+)/

/** A multi-line environment owns its own closing; text after `\end{...}` is not valid there. */
const ENVIRONMENT_TERMINATED = /\\end\{[^}]*\}\s*$/

/**
 * Pull a stranded full stop back into the equation it belongs to.
 *
 * A display equation interrupts its paragraph, so `...is $$f = 1/T$$.` renders the equation
 * as a block and leaves a paragraph containing nothing but `.`, which reads as a typo. Set
 * mathematics has carried its own terminal punctuation since long before the web: the period
 * belongs inside the display, at the end of the last line. That is where this puts it, so
 * the sentence still ends where the author ended it and nothing is silently dropped.
 *
 * The mark goes in as `\text{}` rather than bare. Math mode spaces `:` as a relation and `,`
 * as a separator, which sets a stranded colon a full quad away from the equation it is meant
 * to be touching; text mode sets it tight, the way the same sentence would be set in print.
 */
/** A display token with its closing `$$` row — however the block pads it — removed. */
function terminusStripped(text: string): string {
  return text.replace(/\n[ >\t]*\$\$$/, '')
}

function absorbStrandedPunctuation(tokens: RenderToken[]): RenderToken[] {
  const result = tokens.map((token) => ({ ...token }))
  for (let index = 0; index < result.length - 1; index += 1) {
    const equation = result[index]
    const following = result[index + 1]
    if (!equation.display || following.display) continue
    if (ENVIRONMENT_TERMINATED.test(terminusStripped(equation.text))) continue
    const match = STRANDED_PUNCTUATION.exec(following.text)
    if (!match) continue
    // A function replacement, because `$$` in a replacement string means a literal `$`.
    // The mark is set tight against the equation, the newline before the closing row is
    // consumed, and the block's padding around the closing row is kept.
    equation.text = equation.text.replace(
      /\n([ >\t]*)\$\$$/,
      (_m, pad) => `${pad}\\text{${match[1]}}\n${pad}$$`,
    )
    following.text = following.text.slice(match[0].length)
  }
  return result
}

/**
 * Append a token to the normalized output so far.
 *
 * Plain text is appended verbatim. A display equation is fenced off by blank lines, because
 * that is how it keeps its row to itself. Inside a blockquote the blank line carries the
 * quote's marker, or the equation would fall out of the block it belongs to.
 *
 * The text ahead of a display can already end with the display's own continuation line —
 * the padding of its `$$` row, consumed as plain text before the delimiter. The display
 * re-pads that row, so the duplicate is dropped here; and a line that carries only the
 * block's marker is the blank line the display is fenced by, not a line to fence again.
 * Both are what keep the render copy a fixed point: normalizing it a second time adds
 * nothing.
 */
function appendToken(output: string, token: RenderToken, container?: Container): string {
  if (!token.display) return output + token.text
  const blank = (container?.quotePrefix ?? '').replace(/\s+$/, '')
  let head = output
  const firstLine = token.text.split('\n', 1)[0]
  const lastNL = head.lastIndexOf('\n')
  const lastLine = lastNL === -1 ? head : head.slice(lastNL + 1)
  if (lastLine !== '' && firstLine.startsWith(lastLine)) {
    head = lastNL === -1 ? '' : head.slice(0, lastNL + 1)
  }
  let prefix: string
  if (head.length === 0 || head.endsWith('\n\n')) prefix = ''
  else if (head.endsWith('\n')) {
    const nl = head.lastIndexOf('\n', head.length - 2)
    const before = nl === -1 ? head.slice(0, -1) : head.slice(nl + 1, -1)
    prefix = before.replace(/[> \t]/g, '') === '' ? '' : `${blank}\n`
  } else prefix = `\n${blank}\n`
  return `${head}${prefix}${token.text}\n${blank}\n`
}

/**
 * Repairs only the render copy of a streamed answer. Code remains byte-for-byte intact;
 * synthetic closers are added only while streaming so an incomplete fragment cannot absorb
 * the rest of the Markdown tree.
 *
 * **Math that has not finished arriving is withheld rather than typeset.** Code and prose
 * grow a character at a time and read fine doing it. An equation does not: closing
 * `$\frac{1}{2` synthetically renders a fraction with one arm, and a fragment long enough
 * to look like display math is centred on its own line, only to snap back inline when the
 * closing delimiter arrives. That is what made equations look like they populated ahead of
 * the sentences holding them: they were being drawn out of the text flow before the text
 * existed. Held back, the whole equation enters the reveal cascade in its own place, and
 * the settled render (`streaming` false) prints an unterminated one literally, so nothing
 * is lost either way.
 *
 * The line-wise undelimited repair runs in both modes: while streaming it covers only the
 * lines that have fully arrived, and the settled render covers all of them. Both renders
 * therefore agree on every line they share, and completing a turn adds at most the
 * interpretation of the line that was still arriving — never a switch of the answer-wide
 * rule.
 */
export function normalizeMarkdownForRender(source: string, streaming = false): string {
  const lineEndings = source.replaceAll('\r\n', '\n').replaceAll('\r', '\n')
  // While streaming, only complete lines are repaired: a half-arrived line is wrapped on
  // the chunk in which it completes, and keeps that interpretation afterwards.
  const normalized = repairUndelimitedMath(lineEndings, { completeLinesOnly: streaming })
  const tokens: RenderToken[] = []
  let plain = ''
  let cursor = 0

  const flushPlain = () => {
    addToken(tokens, plain)
    plain = ''
  }

  while (cursor < normalized.length) {
    const fence = lineStartsFence(normalized, cursor)
    if (fence) {
      flushPlain()
      const closing = findFenceEnd(normalized, fence.end, fence.char, fence.length)
      if (closing === null) {
        const raw = normalized.slice(cursor)
        addToken(tokens, streaming ? `${raw}\n${fence.char.repeat(fence.length)}\n` : raw)
        cursor = normalized.length
      } else {
        addToken(tokens, normalized.slice(cursor, closing))
        cursor = closing
      }
      continue
    }

    if (normalized[cursor] === '`' && !isEscaped(normalized, cursor)) {
      let run = 1
      while (normalized[cursor + run] === '`') run += 1
      const closer = '`'.repeat(run)
      const closing = findClosing(normalized, cursor + run, closer)
      if (closing === null) {
        flushPlain()
        const raw = normalized.slice(cursor)
        addToken(tokens, streaming ? `${raw}${closer}` : raw)
        cursor = normalized.length
      } else {
        flushPlain()
        addToken(tokens, normalized.slice(cursor, closing + run))
        cursor = closing + run
      }
      continue
    }

    if (
      (normalized.startsWith('\\(', cursor) || normalized.startsWith('\\[', cursor)) &&
      !isEscaped(normalized, cursor)
    ) {
      const opening = normalized.slice(cursor, cursor + 2)
      const closer = opening === '\\(' ? '\\)' : '\\]'
      const closing = findClosing(normalized, cursor + 2, closer)
      if (closing === null) {
        if (!streaming) {
          plain += normalized[cursor]
          cursor += 1
          continue
        }
        flushPlain()
        cursor = normalized.length
      } else {
        flushPlain()
        const inner = normalized.slice(cursor + 2, closing)
        const display = opening === '\\['
        const container = containerAt(normalized, cursor)
        addToken(
          tokens,
          display ? formatDisplayMath(inner, container) : `$${inner}$`,
          display,
          display ? container : undefined,
        )
        cursor = closing + 2
      }
      continue
    }

    if (normalized[cursor] === '$' && !isEscaped(normalized, cursor)) {
      if (normalized[cursor + 1] === '$') {
        const closing = findClosing(normalized, cursor + 2, '$$')
        if (closing === null) {
          // An open display equation is the strongest signal in the buffer. While the
          // stream is open it is withheld so a half fraction never typesets; once the
          // stream is settled an unmatched `$$` is just two dollars.
          if (!streaming) {
            plain += normalized[cursor]
            cursor += 1
            continue
          }
          flushPlain()
          cursor = normalized.length
        } else {
          flushPlain()
          const container = containerAt(normalized, cursor)
          addToken(
            tokens,
            formatDisplayMath(normalized.slice(cursor + 2, closing), container),
            true,
            container,
          )
          cursor = closing + 2
        }
        continue
      }

      // A single `$`. The span opens only on a dollar followed by a non-space, closes
      // only on a dollar not preceded by a space, and cannot cross a line break — the
      // parser's own delimiter rules, kept in step with it.
      const next = normalized[cursor + 1]
      const lineEnd = normalized.indexOf('\n', cursor + 1)
      const searchEnd = lineEnd === -1 ? normalized.length : lineEnd
      const closing =
        next !== undefined && next !== ' ' && next !== '\n'
          ? findDollarClosing(normalized, cursor + 1, searchEnd)
          : null
      if (closing === null) {
        const lineHasLaterDollar = hasUnescapedDollar(
          normalized,
          cursor + 1,
          lineEnd === -1 ? normalized.length : lineEnd,
        )
        if (!streaming) {
          // Settled: the span never closed, so the dollar is literal — escaped only
          // where the parser would pair it with a later dollar on the line.
          plain += lineHasLaterDollar ? '\\$' : '$'
          cursor += 1
          continue
        }
        // A digit after the dollar is a price, not the start of mathematics: reading
        // `$5 and includes shipping` as an unclosed equation would withhold the rest of
        // the answer for the whole turn. The parser, however, opens a span on any
        // non-space after the dollar, so two prices on one line — `It costs $5 and $10` —
        // would pair the first dollar with the second and typeset the prose between them
        // as an equation. A price another unescaped dollar on the line can close against
        // is therefore escaped in the render copy: the stored text keeps its dollars,
        // and only the rendered glyphs lose the delimiters.
        if (next !== undefined && /[0-9]/.test(next)) {
          plain += lineHasLaterDollar ? '\\$' : '$'
          cursor += 1
          continue
        }
        // A space after the dollar is final: no span can open there.
        if (next === ' ' || next === '\n' || next === undefined) {
          plain += normalized[cursor]
          cursor += 1
          continue
        }
        // A line that has arrived in full without a closer is final: the dollar is
        // literal — escaped only where the parser would pair it with a later dollar on
        // the line — and the rest of the answer shows.
        if (lineEnd !== -1) {
          plain += (lineHasLaterDollar ? '\\$' : '$') + normalized.slice(cursor + 1, lineEnd + 1)
          cursor = lineEnd + 1
          continue
        }
        // An unclosed equation at the tail of an open line: withhold it, and only it.
        flushPlain()
        cursor = normalized.length
      } else {
        flushPlain()
        // A closed explicit span is inline mathematics, and stays inline: a newline
        // after it, or the stream finishing, never re-sorts it into a display block.
        addToken(tokens, `$${normalized.slice(cursor + 1, closing)}$`)
        cursor = closing + 1
      }
      continue
    }

    const environment = normalized.slice(cursor).match(DISPLAY_ENVIRONMENT_PATTERN)
    if (environment && !isEscaped(normalized, cursor)) {
      const name = environment[1]
      const opening = environment[0]
      const closingText = `\\end{${name}}`
      const closing = findClosing(normalized, cursor + opening.length, closingText)
      if (closing === null) {
        if (!streaming) {
          plain += normalized[cursor]
          cursor += 1
          continue
        }
        flushPlain()
        cursor = normalized.length
      } else {
        flushPlain()
        const container = containerAt(normalized, cursor)
        addToken(
          tokens,
          formatDisplayMath(normalized.slice(cursor, closing + closingText.length), container),
          true,
          container,
        )
        cursor = closing + closingText.length
      }
      continue
    }

    plain += normalized[cursor]
    cursor += 1
  }

  flushPlain()
  return absorbStrandedPunctuation(tokens).reduce((output, token, index, tokens) => {
    if (!token.display) {
      // A display's suffix already owns the blank line after it. The source's own
      // terminators at the head of the following text would stack up one per pass, so the
      // render copy is a fixed point: normalizing it again changes nothing. Inside a
      // blockquote the blank line carries the quote's marker, so the marker's line is what
      // the suffix already emitted, and it is what gets dropped.
      if (tokens[index - 1]?.display) {
        const blank = (tokens[index - 1].container?.quotePrefix ?? '').replace(/\s+$/, '')
        let text = token.text
        if (blank !== '' && text.startsWith(`\n${blank}\n`)) text = text.slice(blank.length + 2)
        else text = text.replace(/^\n+/, '')
        return text ? output + text : output
      }
      return output + token.text
    }
    return appendToken(output, token, token.container)
  }, '')
}
