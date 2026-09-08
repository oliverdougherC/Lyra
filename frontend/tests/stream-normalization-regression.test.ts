import { describe, expect, it } from 'vitest'

import { normalizeMarkdownForRender } from '@/components/chat/markdown-utils'

/**
 * Chunk-boundary stability for the streamed normalizer (PLA-500).
 *
 * A streamed answer is normalized once per chunk, and the chunks land at arbitrary points:
 * inside a delimiter, inside a brace, on a line boundary. A completed equation must not
 * change its identity or placement because a later chunk arrived, and a chunk boundary must
 * not turn a completed inline span into a display block (or vice versa).
 */

/** The equations of a normalized render, in order, with their display/inline placement. */
function placements(normalized: string): { display: boolean; inner: string }[] {
  const out: { display: boolean; inner: string }[] = []
  let i = 0
  while (i < normalized.length) {
    const at = normalized.indexOf('$', i)
    if (at === -1) break
    if (normalized[at + 1] === '$') {
      const close = normalized.indexOf('$$', at + 2)
      if (close === -1) break
      out.push({ display: true, inner: normalized.slice(at + 2, close) })
      i = close + 2
    } else {
      const close = normalized.indexOf('$', at + 1)
      if (close === -1) break
      out.push({ display: false, inner: normalized.slice(at + 1, close) })
      i = close + 1
    }
  }
  return out
}

/**
 * For every way of cutting the source at or after `from`, the normalized prefix and the
 * normalized whole must agree on every equation the prefix already completed: same
 * placement, same inner text, same order.
 */
function assertStableAcrossChunkBoundaries(source: string, from: number): void {
  const full = placements(normalizeMarkdownForRender(source))
  for (let at = from; at <= source.length; at += 1) {
    const prefixSource = source.slice(0, at)
    const prefix = placements(normalizeMarkdownForRender(prefixSource, true))
    expect(prefix.length, `too many equations before the cut at ${at}`).toBeLessThanOrEqual(
      full.length,
    )
    for (let k = 0; k < prefix.length; k += 1) {
      expect(prefix[k], `equation ${k} flipped at cut ${at}`).toEqual(full[k])
    }
  }
}

describe('chunk boundaries never change a completed equation', () => {
  it('inline fraction followed by prose stays inline through every cut', () => {
    const source = '- Use $\\frac{1}{2}$ of the sample.'
    // The equation's closing dollar sits at offset 18; from cut 19 on the prefix must
    // agree with the whole. Cuts before the closer may withhold it, never show it as display.
    assertStableAcrossChunkBoundaries(source, 19)
    const full = normalizeMarkdownForRender(source)
    expect(full).not.toContain('$$')
  })

  it('a list with a lifted equation keeps its structure through every cut', () => {
    const source = '- First $\\frac{1}{2}$\n- Second $x$'
    // The first equation closes at offset 20; its line ends at the newline 21, which is
    // the moment the promotion becomes knowable. Every cut at or after 22 must agree
    // with the whole; cuts before may withhold, but never show the equation misplaced.
    assertStableAcrossChunkBoundaries(source, 22)
    const full = normalizeMarkdownForRender(source, true)
    const fullPlacements = placements(full)
    expect(fullPlacements).toHaveLength(2)
    expect(fullPlacements[0].display).toBe(true)
    expect(fullPlacements[1].display).toBe(false)
    // The lifted block is indented into the list item, never root-level.
    for (const line of full.split('\n')) {
      if (line.trim() === '$$') expect(line.startsWith('  ')).toBe(true)
    }
  })

  it('an explicit bracket display stays display through every cut after it closes', () => {
    const source = 'Then \\[x = 1\\] and more.'
    // `\[x = 1\]` closes at offset 14.
    assertStableAcrossChunkBoundaries(source, 14)
  })

  it('a supported environment stays one equation through every cut after it closes', () => {
    const source = 'Start\n\\begin{align}\nx &= 1\n\\end{align}\nThen done.'
    // The environment closes at the end of `\end{align}`.
    const closesAt = source.indexOf('\\end{align}') + '\\end{align}'.length
    assertStableAcrossChunkBoundaries(source, closesAt)
    const full = placements(normalizeMarkdownForRender(source, true))
    expect(full).toHaveLength(1)
    expect(full[0].display).toBe(true)
  })

  it('does not flip a later equation when an earlier one is cut mid-brace', () => {
    const source = 'a $\\frac{1}{2}$ b $\\frac{3}{4}$ c'
    // The first equation closes at offset 14. Every cut after that keeps it inline, and
    // a cut inside the second equation never surfaces the second equation at all.
    const firstClose = source.indexOf('$\\frac{1}{2}$') + '$\\frac{1}{2}$'.length
    const secondOpen = source.indexOf('$\\frac{3}{4}$')
    assertStableAcrossChunkBoundaries(source, firstClose)
    const midCut = normalizeMarkdownForRender(source.slice(0, secondOpen + 5), true)
    expect(placements(midCut)).toHaveLength(1)
  })

  it('currencies never withhold the prose after them at any cut', () => {
    const source = 'It costs $5 and includes shipping.'
    // From the moment the digit after the dollar has arrived, the whole received text must
    // be visible: the dollar is currency and nothing after it is withheld.
    for (let at = 11; at <= source.length; at += 1) {
      const normalized = normalizeMarkdownForRender(source.slice(0, at), true)
      expect(normalized, `prose withheld at cut ${at}`).toBe(source.slice(0, at))
    }
  })

  it('mixed price and math never withholds an unrelated sentence at any cut', () => {
    const source = 'Costs $5 and $\\frac{1}{2}$ of it, plus $10 more.'
    for (let at = 0; at <= source.length; at += 1) {
      const prefix = source.slice(0, at)
      const normalized = normalizeMarkdownForRender(prefix, true)
      // The normalizer never reorders or drops arrived text: it may only withhold a tail
      // region of an equation that has not finished arriving.
      expect(prefix.startsWith(normalized), `normalized tail dropped at cut ${at}`).toBe(true)
      // Once the first price has arrived it is visible as literal prose.
      if (prefix.includes('$5')) expect(normalized).toContain('$5')
      // Once the equation has closed it is typeset inline and never as a display block.
      if (prefix.includes('$\\frac{1}{2}$')) {
        expect(normalized).toContain('$\\frac{1}{2}$')
        expect(normalized).not.toContain('$$')
      }
    }
  })

  it('completing the turn changes nothing once every line has arrived', () => {
    // The repair is line-local and applied in streaming and settled alike, so a turn that
    // ends with every line complete does not re-interpret the answer at finalization.
    const source = 'It costs $5, and (a) x(t) = \\frac{1}{t}\nthen settles.'
    const streamed = normalizeMarkdownForRender(source, true)
    expect(normalizeMarkdownForRender(source)).toBe(streamed)
  })
})
