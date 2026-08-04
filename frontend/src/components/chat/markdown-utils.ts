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
  const normalized = source.replaceAll('\r\n', '\n').replaceAll('\r', '\n')
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
        const inner = normalized.slice(cursor + 2)
        addToken(
          tokens,
          opening === '\\(' ? `$${inner}$` : formatDisplayMath(inner),
          opening === '\\[',
        )
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
        const inner = normalized.slice(cursor + delimiter.length)
        addToken(
          tokens,
          display || promote(inner) ? formatDisplayMath(inner) : `$${inner}$`,
          display || promote(inner),
        )
        cursor = normalized.length
      } else {
        flushPlain()
        const inner = normalized.slice(cursor + delimiter.length, closing)
        const promoted = !display && promote(inner)
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
        const inner = normalized.slice(cursor) + `\n${closingText}`
        addToken(tokens, formatDisplayMath(inner), true)
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
