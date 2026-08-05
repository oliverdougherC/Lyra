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

const DISPLAY_COMMAND_PATTERN = /\\(?:frac|int|sum|prod|lim|partial|sqrt|begin)(?:\b|\s|\{)/

type RenderToken = {
  text: string
  display?: boolean
}

function isEscaped(source: string, index: number): boolean {
  let slashes = 0
  for (let cursor = index - 1; cursor >= 0 && source[cursor] === '\\'; cursor -= 1) {
    slashes += 1
  }
  return slashes % 2 === 1
}

function lineStartsFence(
  source: string,
  index: number,
): { end: number; char: '`' | '~'; length: number } | null {
  if (index > 0 && source[index - 1] !== '\n') return null

  let cursor = index
  let indent = 0
  while (indent < 3 && source[cursor] === ' ') {
    cursor += 1
    indent += 1
  }
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
    let cursor = lineStart
    let indent = 0
    while (indent < 3 && source[cursor] === ' ') {
      cursor += 1
      indent += 1
    }

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

function formatDisplayMath(content: string): string {
  const inner = content.trim()
  return `$$\n${inner}\n$$`
}

function shouldPromoteInlineMath(content: string): boolean {
  return content.length > 32 || DISPLAY_COMMAND_PATTERN.test(content)
}

/** Nothing after an equation but the punctuation that ends the sentence it closed. */
const ONLY_PUNCTUATION = /^\s*[.,;:!?]*\s*$/

/** Whether an unescaped `$` — the start of other mathematics — sits in `[from, to)`. */
function hasMathBefore(source: string, from: number, to: number): boolean {
  for (let at = source.indexOf('$', from); at !== -1 && at < to; at = source.indexOf('$', at + 1)) {
    if (!isEscaped(source, at)) return true
  }
  return false
}

/**
 * Whether the span between `start` and `end` has its line to itself.
 *
 * Promotion moves an equation out of the paragraph and onto a row of its own, which is right
 * when the equation is the point of the line and wrong when it is a phrase inside a sentence.
 * An answer reading `(a) $y = ...$; (b) $y = ...$; (c) ...` is the case that made this
 * necessary: one of the five ran past the inline ceiling, so one of the five was lifted out,
 * centred, and left the sentence broken around it while its siblings stayed in the text.
 *
 * Two conditions, and the second is what keeps a list from being singled out by its last
 * member: nothing may follow the span on its line but the punctuation that ends the sentence,
 * and no other mathematics may precede it there. A sentence that simply ends on an equation —
 * `Therefore $y(t) = ...$.` — satisfies both and is still given its own row.
 */
function ownsItsLine(source: string, start: number, end: number): boolean {
  const lineStart = source.lastIndexOf('\n', start - 1) + 1
  const lineEnd = source.indexOf('\n', end)
  const after = source.slice(end, lineEnd === -1 ? source.length : lineEnd)
  return ONLY_PUNCTUATION.test(after) && !hasMathBefore(source, lineStart, start)
}

/** LaTeX commands common enough in an answer that seeing one means mathematics. */
const MATH_COMMAND =
  /\\(?:d?frac|int|iint|oint|sum|prod|sqrt|left|right|cdot|times|div|infty|partial|nabla|lim|log|ln|sin|cos|tan|sec|csc|cot|sinh|cosh|tanh|exp|alpha|beta|gamma|delta|epsilon|zeta|eta|theta|kappa|lambda|mu|nu|xi|pi|rho|sigma|tau|phi|chi|psi|omega|Gamma|Delta|Theta|Lambda|Sigma|Phi|Psi|Omega|neq|leq|geq|ll|gg|approx|equiv|propto|pm|mp|to|rightarrow|Rightarrow|text|mathrm|mathbb|hat|bar|tilde|vec|overline|underline|langle|rangle|lfloor|rfloor|quad|qquad)\b/

/** A leading enumeration label: `(a)`, `a)`, `1.`, `(iii)`. Kept out of the math. */
const LEADING_LABEL = /^(\s*\(?[0-9a-z]{1,4}[).]\s*)/i

/**
 * A fence opened with tildes rather than backticks, which is the same rule
 * `lineStartsFence` reads: up to three spaces of indent, then three or more tildes.
 *
 * Matched on its own rather than folded into the backtick guard below, because a bare `~`
 * is ordinary text — "~10 minutes" — and refusing the repair on one would disable it for
 * prose that has nothing to do with code.
 */
const TILDE_FENCE = /^ {0,3}~{3,}/m

/**
 * Wrap mathematics a model wrote without delimiters.
 *
 * `"answer": "(a) x(t) = \\frac{1}{2\\pi(2-jt)}"` is a real reply, and without `$` around
 * it the student is shown the characters the model typed instead of a fraction. The prompt
 * asks for the delimiters and mostly gets them; this is what happens the rest of the time.
 *
 * Every guard here exists to keep the repair away from text it would damage. It runs only
 * when the source contains no `$`, no backtick, no tilde fence, and no `\(` or `\[` — that
 * is, only when there is no delimited mathematics and no code for a stray `$` to break, so
 * the source cannot already be rendering correctly. Within that, only lines carrying a
 * known LaTeX command are touched.
 *
 * The tilde fence is a guard in its own right because the backtick one does not imply it.
 * A `~~~` block holding a LaTeX command, in a reply with no `$` and no backtick anywhere,
 * was rewritten to `$\frac{1}{2}$` inside the code block: the one thing this module
 * promises never to touch.
 */
export function repairUndelimitedMath(source: string): string {
  if (source.includes('$') || source.includes('`')) return source
  if (TILDE_FENCE.test(source)) return source
  if (source.includes('\\(') || source.includes('\\[')) return source
  // A `\begin{align*}` block already delimits itself and the tokenizer below promotes it.
  if (source.includes('\\begin{')) return source
  if (!MATH_COMMAND.test(source)) return source

  return source
    .split('\n')
    .map((line) => {
      if (!MATH_COMMAND.test(line)) return line
      const prefix = LEADING_LABEL.exec(line)?.[1] ?? ''
      const rest = line.slice(prefix.length).trim()
      return rest ? `${prefix}$${rest}$` : line
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

function addToken(tokens: RenderToken[], text: string, display = false): void {
  if (!text) return
  tokens.push({ text, ...(display ? { display: true } : {}) })
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
function absorbStrandedPunctuation(tokens: RenderToken[]): RenderToken[] {
  const result = tokens.map((token) => ({ ...token }))
  for (let index = 0; index < result.length - 1; index += 1) {
    const equation = result[index]
    const following = result[index + 1]
    if (!equation.display || following.display) continue
    if (ENVIRONMENT_TERMINATED.test(equation.text.replace(/\n\$\$$/, ''))) continue
    const match = STRANDED_PUNCTUATION.exec(following.text)
    if (!match) continue
    // A function replacement, because `$$` in a replacement string means a literal `$`.
    equation.text = equation.text.replace(/\n\$\$$/, () => `\\text{${match[1]}}\n$$`)
    following.text = following.text.slice(match[0].length)
  }
  return result
}

function appendToken(output: string, token: RenderToken): string {
  if (!token.display) return output + token.text
  const prefix = output.length > 0 && !output.endsWith('\n\n') ? '\n\n' : ''
  return `${output}${prefix}${token.text}\n\n`
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
 */
export function normalizeMarkdownForRender(
  source: string,
  streaming = false,
  { promoteInlineMath = true }: { promoteInlineMath?: boolean } = {},
): string {
  // Promotion suits an answer, where a long expression deserves its own line. It does not
  // suit a problem statement in a list row: `$x(t) = \sin(t)[u(t+1) - u(t-1)]$` is over
  // the length threshold, and centring it on its own line while its four siblings sit
  // inline makes one sub-part look like a different kind of thing.
  const promote = promoteInlineMath ? shouldPromoteInlineMath : () => false
  const lineEndings = source.replaceAll('\r\n', '\n').replaceAll('\r', '\n')
  // Not while streaming: a line that has not finished arriving would be wrapped, rendered,
  // and unwrapped again on the next frame, which flickers the equation in and out.
  const normalized = streaming ? lineEndings : repairUndelimitedMath(lineEndings)
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
        addToken(
          tokens,
          opening === '\\(' ? `$${inner}$` : formatDisplayMath(inner),
          opening === '\\[',
        )
        cursor = closing + 2
      }
      continue
    }

    if (normalized[cursor] === '$' && !isEscaped(normalized, cursor)) {
      const display = normalized[cursor + 1] === '$'
      const delimiter = display ? '$$' : '$'
      const closing = findClosing(normalized, cursor + delimiter.length, delimiter)
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
        const inner = normalized.slice(cursor + delimiter.length, closing)
        const promoted =
          !display && promote(inner) && ownsItsLine(normalized, cursor, closing + delimiter.length)
        addToken(
          tokens,
          display || promoted ? formatDisplayMath(inner) : `$${inner}$`,
          display || promoted,
        )
        cursor = closing + delimiter.length
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
        addToken(
          tokens,
          formatDisplayMath(normalized.slice(cursor, closing + closingText.length)),
          true,
        )
        cursor = closing + closingText.length
      }
      continue
    }

    plain += normalized[cursor]
    cursor += 1
  }

  flushPlain()
  return absorbStrandedPunctuation(tokens).reduce(appendToken, '')
}
