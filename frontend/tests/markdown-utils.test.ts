import { describe, expect, it } from 'vitest'

import {
  normalizeMarkdownForRender,
  repairLabelMath,
  repairUndelimitedMath,
} from '@/components/chat/markdown-utils'

/**
 * Contracts from docs/ui-phase-1.md: display math sits on its own blank-line-separated rows,
 * `$...$` is reserved for short inline quantities, and code is never rewritten. Synthetic
 * closers exist so a half-arrived fragment cannot swallow the rest of the document, and they
 * are a streaming-only repair.
 */
describe('normalizeMarkdownForRender', () => {
  describe('plain text', () => {
    it('passes prose through untouched', () => {
      expect(normalizeMarkdownForRender('Just a sentence.')).toBe('Just a sentence.')
    })

    it('normalizes CRLF and lone CR to LF', () => {
      expect(normalizeMarkdownForRender('one\r\ntwo\rthree')).toBe('one\ntwo\nthree')
    })

    it('leaves an empty source empty', () => {
      expect(normalizeMarkdownForRender('')).toBe('')
    })
  })

  describe('code is never rewritten', () => {
    it('leaves a closed fence byte-for-byte intact', () => {
      const source = '```js\nconst a = 1\n```\n'
      expect(normalizeMarkdownForRender(source)).toBe(source)
    })

    it('does not treat math delimiters inside a fence as math', () => {
      const source = '```\n$$x$$ and \\[y\\]\n```\n'
      expect(normalizeMarkdownForRender(source)).toBe(source)
    })

    it('does not treat math delimiters inside inline code as math', () => {
      expect(normalizeMarkdownForRender('use `$$x$$` here')).toBe('use `$$x$$` here')
    })

    it('leaves a tilde fence intact', () => {
      const source = '~~~python\nx = 1\n~~~\n'
      expect(normalizeMarkdownForRender(source)).toBe(source)
    })

    it('leaves a tilde fence holding a LaTeX command intact', () => {
      // The case above passes for the wrong reason: `x = 1` carries no LaTeX command, so
      // the undelimited-math repair declines it whatever the fence is made of. Only a
      // tilde fence whose contents look like mathematics reaches the guard, and until one
      // did, `\frac` inside a `~~~` block was rewritten to `$\frac{1}{2}$` in the code.
      const source = '~~~\n\\frac{1}{2}\n~~~\n'
      expect(normalizeMarkdownForRender(source)).toBe(source)
    })

    it('still repairs undelimited math beside an ordinary tilde', () => {
      // The guard is a tilde *fence*, not a tilde: `~` is ordinary text, and refusing the
      // repair on one would disable it for prose that has nothing to do with code. The
      // whole line is wrapped, which is what this function does to a line of mathematics;
      // `repairLabelMath` is the one that wraps spans and leaves words alone.
      expect(repairUndelimitedMath('about ~10 terms, so \\frac{1}{2}')).toBe(
        '$about ~10 terms, so \\frac{1}{2}$',
      )
    })
  })

  describe('synthetic closers are streaming-only', () => {
    it('closes an unterminated fence while streaming', () => {
      expect(normalizeMarkdownForRender('```js\nconst a = 1', true)).toBe(
        '```js\nconst a = 1\n```\n',
      )
    })

    it('leaves an unterminated fence alone when settled', () => {
      expect(normalizeMarkdownForRender('```js\nconst a = 1')).toBe('```js\nconst a = 1')
    })

    it('closes unterminated inline code while streaming', () => {
      expect(normalizeMarkdownForRender('call `foo', true)).toBe('call `foo`')
    })

    it('leaves unterminated inline code alone when settled', () => {
      expect(normalizeMarkdownForRender('call `foo')).toBe('call `foo')
    })

    it('leaves an unterminated dollar as a literal when settled', () => {
      expect(normalizeMarkdownForRender('costs $5 and')).toBe('costs $5 and')
    })
  })

  describe('math is withheld until it has finished arriving', () => {
    /**
     * Unlike code and prose, a half-arrived equation cannot be shown while it grows: it
     * gets typeset out of the text flow, ahead of the sentence holding it, and snaps back
     * when the closing delimiter lands. Holding it lets the whole equation enter the
     * reveal cascade in its own place.
     */
    it('holds back an unfinished dollar equation while streaming', () => {
      expect(normalizeMarkdownForRender('we obtain $$\\frac{1}{2', true)).toBe('we obtain ')
    })

    it('holds back an unfinished bracket equation while streaming', () => {
      expect(normalizeMarkdownForRender('so \\[x = ', true)).toBe('so ')
    })

    it('holds back an unfinished environment while streaming', () => {
      expect(normalizeMarkdownForRender('then \\begin{align}\nx &= 1', true)).toBe('then ')
    })

    it('renders it whole as soon as the closing delimiter arrives', () => {
      expect(normalizeMarkdownForRender('we obtain $$\\frac{1}{2}$$', true)).toBe(
        'we obtain \n\n$$\n\\frac{1}{2}\n$$\n\n',
      )
    })
  })

  describe('LaTeX delimiters become dollar math', () => {
    it('converts inline \\( \\) to single dollars', () => {
      expect(normalizeMarkdownForRender('let \\(x\\) be')).toBe('let $x$ be')
    })

    it('converts display \\[ \\] to a blank-line-separated block', () => {
      expect(normalizeMarkdownForRender('so \\[x = 1\\] then')).toBe(
        'so \n\n$$\nx = 1\n$$\n\n then',
      )
    })
  })

  describe('inline math promotion', () => {
    it('keeps a short quantity inline', () => {
      expect(normalizeMarkdownForRender('where $x$ is')).toBe('where $x$ is')
    })

    it('promotes inline math carrying a display command', () => {
      expect(normalizeMarkdownForRender('$\\frac{1}{2}$')).toBe('$$\n\\frac{1}{2}\n$$\n\n')
    })

    it('promotes inline math longer than the inline ceiling', () => {
      const long = 'a'.repeat(33)
      expect(normalizeMarkdownForRender(`$${long}$`)).toBe(`$$\n${long}\n$$\n\n`)
    })

    it('always treats double dollars as display', () => {
      expect(normalizeMarkdownForRender('$$x$$')).toBe('$$\nx\n$$\n\n')
    })

    it('promotes an equation the sentence ends on', () => {
      const long = 'a'.repeat(33)
      expect(normalizeMarkdownForRender(`Therefore $${long}$.`)).toBe(
        `Therefore \n\n$$\n${long}\\text{.}\n$$\n\n`,
      )
    })

    it('leaves an equation inline when the sentence carries on past it', () => {
      const long = 'a'.repeat(33)
      const source = `(a) $${long}$; (b) $x$`
      expect(normalizeMarkdownForRender(source)).toBe(source)
    })

    it('leaves the last of a run of equations inline with its siblings', () => {
      const long = 'a'.repeat(33)
      const source = `(a) $x$; (b) $${long}$.`
      expect(normalizeMarkdownForRender(source)).toBe(source)
    })
  })

  describe('display environments', () => {
    it('wraps a bare align environment in display delimiters', () => {
      expect(normalizeMarkdownForRender('\\begin{align}\nx &= 1\n\\end{align}')).toBe(
        '$$\n\\begin{align}\nx &= 1\n\\end{align}\n$$\n\n',
      )
    })

    it('wraps a starred environment', () => {
      expect(normalizeMarkdownForRender('\\begin{align*}\nx\n\\end{align*}')).toBe(
        '$$\n\\begin{align*}\nx\n\\end{align*}\n$$\n\n',
      )
    })
  })

  describe('stranded punctuation', () => {
    it('pulls a trailing full stop into the equation as text', () => {
      expect(normalizeMarkdownForRender('is $$f = 1/T$$.')).toBe(
        'is \n\n$$\nf = 1/T\\text{.}\n$$\n\n',
      )
    })

    it('pulls punctuation back across a single line break', () => {
      expect(normalizeMarkdownForRender('is $$f$$\n,')).toBe('is \n\n$$\nf\\text{,}\n$$\n\n')
    })

    it('leaves a new paragraph alone', () => {
      // A blank line is a paragraph the author started, not a fallen-off full stop.
      const result = normalizeMarkdownForRender('is $$f$$\n\n. next')
      expect(result).not.toContain('\\text{.}')
      expect(result).toContain('. next')
    })

    it('does not absorb into an environment that closes itself', () => {
      const result = normalizeMarkdownForRender('\\begin{align}\nx\n\\end{align}.')
      expect(result).not.toContain('\\text{.}')
    })
  })

  describe('escaping', () => {
    it('treats an escaped dollar as a literal', () => {
      expect(normalizeMarkdownForRender('costs \\$5 today')).toBe('costs \\$5 today')
    })
  })

  describe('inline math is never promoted from a transient stream tail', () => {
    // PLA-500 R1: a closed inline span at the end of the received text is not at the end of
    // its line. Promoting it there moved a completed equation from block to inline (or the
    // reverse) the moment the next chunk arrived.

    it('keeps a closed fraction inline while the sentence is still arriving', () => {
      const prefix = '- Use $\\frac{1}{2}$'
      expect(normalizeMarkdownForRender(prefix, true)).toBe(prefix)
    })

    it('keeps it inline when the sentence then continues', () => {
      expect(normalizeMarkdownForRender('- Use $\\frac{1}{2}$ of the sample.', true)).toBe(
        '- Use $\\frac{1}{2}$ of the sample.',
      )
    })

    it('does not flip the placement between the prefix and the completed sentence', () => {
      const prefix = normalizeMarkdownForRender('- Use $\\frac{1}{2}$', true)
      const full = normalizeMarkdownForRender('- Use $\\frac{1}{2}$ of the sample.', true)
      // No display block appears and disappears: both renders hold one inline span.
      expect(prefix).not.toContain('$$')
      expect(full).not.toContain('$$')
      expect(full).toContain('$\\frac{1}{2}$')
    })

    it('still promotes a sentence-final equation once the answer is settled', () => {
      // Settlement is when the stream has stopped growing: the line end is real, so the
      // documented sentence-end promotion applies and the container is still preserved.
      expect(normalizeMarkdownForRender('Therefore $\\frac{1}{2}$.')).toBe(
        'Therefore \n\n$$\n\\frac{1}{2}\\text{.}\n$$\n\n',
      )
    })

    it('promotes a closed equation only after its line break has arrived', () => {
      // The newline is the structural boundary, and once received it never un-arrives.
      const open = '- Use $\\frac{1}{2}$'
      const withBreak = '- Use $\\frac{1}{2}$\n'
      expect(normalizeMarkdownForRender(open, true)).toBe(open)
      expect(normalizeMarkdownForRender(withBreak, true)).toBe(
        '- Use \n\n  $$\n  \\frac{1}{2}\n  $$\n\n',
      )
    })
  })

  describe('display math stays inside the list and blockquote that holds it', () => {
    // PLA-500 R2: an equation lifted out of a list item used to land at root level, which
    // closed the first list and started a second one around it.

    it('keeps a promoted equation inside the list item', () => {
      const source = '- First $\\frac{1}{2}$\n- Second $x$'
      for (const streaming of [true, false]) {
        const normalized = normalizeMarkdownForRender(source, streaming)
        // Both items survive on their markers, and the display block is indented into the
        // item rather than sitting at the root of the document.
        expect(normalized).toContain('- First')
        expect(normalized).toContain('\n- Second $x$')
        const displayLines = normalized.split('\n').filter((line) => line.trim() === '$$')
        expect(displayLines).toHaveLength(2)
        for (const line of displayLines) {
          expect(line.startsWith('  ')).toBe(true)
        }
      }
    })

    it('renders as one list, not two', () => {
      // The containment is what the browser and CommonMark see: the normalized output of a
      // two-item list must not contain a root-level display block between the items.
      const source = '- First $\\frac{1}{2}$\n- Second $x$'
      const normalized = normalizeMarkdownForRender(source, true)
      expect(normalized).toBe('- First \n\n  $$\n  \\frac{1}{2}\n  $$\n\n- Second $x$')
    })

    it('keeps a promoted equation inside a blockquoted list item', () => {
      const source = '> - item $\\frac{1}{2}$'
      const normalized = normalizeMarkdownForRender(source, false)
      // Every line of the lifted equation carries the blockquote marker, so the blockquote
      // and the list inside it survive.
      expect(normalized).toBe('> - item \n>\n>   $$\n>   \\frac{1}{2}\n>   $$\n>\n')
    })

    it('keeps a promoted equation inside a nested list item', () => {
      const source = '- one\n  - two $\\frac{1}{2}$\n- three'
      const normalized = normalizeMarkdownForRender(source, false)
      expect(normalized).toBe('- one\n  - two \n\n    $$\n    \\frac{1}{2}\n    $$\n\n- three')
    })

    it('keeps a promoted equation inside an ordered list item', () => {
      const source = '1. alpha $\\frac{1}{2}$\n2. beta $x$'
      const normalized = normalizeMarkdownForRender(source, false)
      expect(normalized).toBe('1. alpha \n\n   $$\n   \\frac{1}{2}\n   $$\n\n2. beta $x$')
    })
  })

  describe('a lone dollar is currency, not an unclosed equation', () => {
    // PLA-500 R3: an unescaped `$` without a closing `$` used to withhold everything after
    // it, so `It costs $5 and includes shipping.` read as `It costs ` for the whole turn.

    it('renders a price as literal prose while streaming', () => {
      expect(normalizeMarkdownForRender('It costs $5 and includes shipping.', true)).toBe(
        'It costs $5 and includes shipping.',
      )
    })

    it('renders the price identically when settled', () => {
      expect(normalizeMarkdownForRender('It costs $5 and includes shipping.')).toBe(
        'It costs $5 and includes shipping.',
      )
    })

    it('coexists with real mathematics in the same sentence', () => {
      const source = 'Costs $5 and $\\frac{1}{2}$ of it, plus $10 more.'
      expect(normalizeMarkdownForRender(source, true)).toBe(source)
    })

    it('treats a dollar followed by a space as a literal, like the parser does', () => {
      expect(normalizeMarkdownForRender('the $ 5 special', true)).toBe('the $ 5 special')
    })

    it('releases a non-currency dollar once its line has arrived complete', () => {
      // remark-math never spans a line break, so a `$` whose line has closed with no
      // closer is final: literal, and the rest of the answer is not held back.
      const source = 'price is $x and ships free\nnext line'
      expect(normalizeMarkdownForRender(source, true)).toBe(source)
    })

    it('still withholds a genuinely incomplete equation at the stream tail', () => {
      expect(normalizeMarkdownForRender('price is $x and ships fre', true)).toBe('price is ')
    })

    it('still withholds an unfinished display equation at the stream tail', () => {
      expect(normalizeMarkdownForRender('we obtain $$\\frac{1}{2', true)).toBe('we obtain ')
    })

    it('does not open a span on a dollar followed by a space', () => {
      // remark-math requires a non-space after the opening dollar; the normalizer must
      // agree, or it would typeset ` 5 and ` as mathematics.
      expect(normalizeMarkdownForRender('pay $ 5 today', true)).toBe('pay $ 5 today')
    })

    it('does not close a span on a dollar preceded by a space', () => {
      // The closing dollar must be touching the mathematics, as in the parser.
      expect(normalizeMarkdownForRender('a $b $ c$ d', true)).toBe('a $b $ c$ d')
    })
  })

  describe('undelimited math is repaired line by line, not only at completion', () => {
    // The settled render used to run the whole-answer repair only when the turn ended, so
    // the answer jumped from literal prose to typeset mathematics at finalization. The
    // repair now applies per line as each line completes, in streaming and settled alike.

    it('repairs a completed line while streaming and defers the open line', () => {
      const source = '(a) x(t) = \\frac{1}{t}\n(b) y = 2'
      expect(normalizeMarkdownForRender(source, true)).toBe(
        '(a) \n\n$$\nx(t) = \\frac{1}{t}\n$$\n\n(b) y = 2',
      )
    })

    it('does not wrap a line that is still arriving', () => {
      // A half-arrived line must not be wrapped: the synthetic closer would typeset a
      // fraction with one arm. The line completes on the next chunk and is wrapped then.
      const source = '(a) x(t) = \\frac{1}{t}'
      expect(normalizeMarkdownForRender(source, true)).toBe(source)
      expect(normalizeMarkdownForRender(source)).toBe('(a) \n\n$$\nx(t) = \\frac{1}{t}\n$$\n\n')
    })

    it('uses compatible normalization when the turn completes', () => {
      // Once every line of the source has arrived, the streaming and settled renders agree:
      // completing a turn is not a moment when the answer-wide interpretation switches on.
      const source = '(a) x(t) = \\frac{1}{t}\n(b) y = 2\n'
      expect(normalizeMarkdownForRender(source, true)).toBe(normalizeMarkdownForRender(source))
    })

    it('is idempotent over its own repaired output', () => {
      const source = '(a) x(t) = \\frac{1}{t}\n(b) y = 2\n'
      const once = normalizeMarkdownForRender(source, true)
      expect(normalizeMarkdownForRender(once, true)).toBe(once)
    })

    it('is a fixed point for a display equation lifted into a blockquote or list', () => {
      // The lifted block carries the container's marker on every row, and re-normalizing
      // the render copy must add nothing: the delimiters and blank lines are read where they
      // sit, so a blockquote-prefixed `$$` row is a fence, not bare math to re-wrap.
      const quote = '> - Quoted item with $\\zeta=\\frac{1}{2}$\n'
      const list = '- item with a display fraction\n  $$\\frac{1}{s+2}$$\n  still inside\n- next\n'
      for (const source of [quote, list]) {
        const once = normalizeMarkdownForRender(source, false)
        expect(
          normalizeMarkdownForRender(once, false),
          `not a fixed point: ${JSON.stringify(once)}`,
        ).toBe(once)
        expect(normalizeMarkdownForRender(once, true)).toBe(once)
      }
    })

    it('leaves an already-delimited line alone even when its sibling line is bare', () => {
      // The delimited line keeps its own delimiters; the bare line is wrapped as a whole
      // line, which the tokenizer then holds inline while the stream is open.
      const source = 'First $\\frac{1}{2}$, then \\frac{3}{4} outside.\nBare x = \\frac{1}{2} here.'
      expect(normalizeMarkdownForRender(source, true)).toBe(source)
    })
  })

  describe('block separation', () => {
    it('does not stack blank lines when display math already sits on its own row', () => {
      expect(normalizeMarkdownForRender('text\n\n$$x$$')).toBe('text\n\n$$\nx\n$$\n\n')
    })

    it('separates two consecutive equations', () => {
      expect(normalizeMarkdownForRender('$$a$$$$b$$')).toBe('$$\na\n$$\n\n$$\nb\n$$\n\n')
    })
  })
})

/**
 * A model asked for LaTeX delimiters mostly supplies them. The rest of the time the
 * student is shown `\frac{1}{2\pi(2-jt)}` as characters, which is worse than useless in a
 * field labelled "Answer". Every guard below exists to keep this repair away from text it
 * would damage.
 */
describe('repairUndelimitedMath', () => {
  it('wraps a bare expression, keeping its label outside the math', () => {
    expect(repairUndelimitedMath('(b) x(t) = \\frac{1}{2\\pi(2-jt)}')).toBe(
      '(b) $x(t) = \\frac{1}{2\\pi(2-jt)}$',
    )
  })

  it('wraps each line of a multi-line answer on its own', () => {
    const source = '(a) x(t) = \\frac{1}{t}\n(b) x(t) = \\cos(\\pi t)'

    expect(repairUndelimitedMath(source)).toBe(
      '(a) $x(t) = \\frac{1}{t}$\n(b) $x(t) = \\cos(\\pi t)$',
    )
  })

  it('leaves a source that already has delimiters completely alone', () => {
    // The one signal that the model did its job. Touching anything here risks breaking
    // mathematics that was already rendering.
    const source = 'First $\\frac{1}{2}$, then \\frac{3}{4} outside.'

    expect(repairUndelimitedMath(source)).toBe(source)
  })

  it('leaves prose alone', () => {
    const source = 'The system is stable because every pole lies in the left half plane.'

    expect(repairUndelimitedMath(source)).toBe(source)
  })

  it('does not touch a source containing code', () => {
    // A `$` inserted into a shell snippet is a variable expansion, not a delimiter.
    const source = 'Run `printf \\frac` to see it.'

    expect(repairUndelimitedMath(source)).toBe(source)
  })

  it('does not touch bracket-delimited math it would break', () => {
    const source = '\\[ \\frac{1}{2} \\]'

    expect(repairUndelimitedMath(source)).toBe(source)
  })

  it('leaves a line with no mathematics untouched inside an answer that has some', () => {
    const source = 'Both parts converge.\n(a) x = \\frac{1}{2}'

    expect(repairUndelimitedMath(source)).toBe('Both parts converge.\n(a) $x = \\frac{1}{2}$')
  })
})

/**
 * A step title is prose with mathematics in it, which is neither of the two things
 * `repairUndelimitedMath` handles. Wrapping the whole line typesets the words; wrapping
 * nothing prints the LaTeX source at the top of a step whose body renders properly.
 */
describe('repairLabelMath', () => {
  it('wraps the mathematics and leaves the words alone', () => {
    expect(repairLabelMath('Part (a) Convolution of u(t) and e^{-t}u(t)')).toBe(
      'Part (a) Convolution of $u(t)$ and $e^{-t}u(t)$',
    )
  })

  it('keeps an enumeration label out of the mathematics', () => {
    // `(a)` is a bracket with no function in front of it. Typeset, it would become an
    // italic variable in the middle of a heading.
    expect(repairLabelMath('Part (c) Convolution of u(t-1) and u(t)')).toBe(
      'Part (c) Convolution of $u(t-1)$ and $u(t)$',
    )
  })

  it('carries an operator that joins two expressions into one span', () => {
    expect(repairLabelMath('Properties of h(t) = e^{t}u(-t)')).toBe(
      'Properties of $h(t) = e^{t}u(-t)$',
    )
  })

  it('leaves a label that delimited its own mathematics untouched', () => {
    const already = 'Determine $h(t)$ for system (a)'

    expect(repairLabelMath(already)).toBe(already)
  })

  it.each([
    'Answer',
    'Set up the convolution integral.',
    'Linearity and Time-Invariance',
    'Step 1: apply the sifting property.',
  ])('leaves prose with no mathematics in it exactly as it was: %s', (label) => {
    expect(repairLabelMath(label)).toBe(label)
  })

  it('leaves the sentence its closing punctuation', () => {
    expect(repairLabelMath('Compute X(jw) for the signal.')).toBe('Compute $X(jw)$ for the signal.')
    expect(repairLabelMath('Evaluate u(t).')).toBe('Evaluate $u(t)$.')
  })

  it('wraps a LaTeX command it finds undelimited', () => {
    expect(repairLabelMath('Part (d) Convolution of \\delta(t-2) and e^{-t}u(t)')).toBe(
      'Part (d) Convolution of $\\delta(t-2)$ and $e^{-t}u(t)$',
    )
  })
})
