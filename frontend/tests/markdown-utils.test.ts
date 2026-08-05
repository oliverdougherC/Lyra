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
