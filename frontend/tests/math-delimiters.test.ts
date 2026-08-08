/**
 * The draft body's math delimiters, and the two things that must survive them.
 *
 * The mirror of `backend/tests/test_mathnorm.py` - both sides convert the same text the
 * same way, because a body is normalized server-side where AI text lands and client-side
 * when a legacy body is loaded, and a disagreement between them would rewrite the
 * document back and forth on every visit.
 */

import { describe, expect, it } from 'vitest'

import { normalizeMathDelimiters } from '@/lib/drafts/math-delimiters'

describe('normalizeMathDelimiters', () => {
  it('converts LaTeX inline delimiters to dollars', () => {
    expect(normalizeMathDelimiters(String.raw`The set \(\{u_1, u_k\}\) is independent.`)).toBe(
      'The set $\\{u_1, u_k\\}$ is independent.',
    )
  })

  it('converts LaTeX display delimiters to double dollars', () => {
    const normalized = normalizeMathDelimiters(String.raw`Solve \[ A\mathbf{x} = \mathbf{0} \].`)

    expect(normalized).toContain('$$\nA\\mathbf{x} = \\mathbf{0}\n$$')
    expect(normalized).not.toContain('\\[')
  })

  it('keeps two spans on one line as two spans', () => {
    expect(normalizeMathDelimiters(String.raw`\(a\) and \(b\)`)).toBe('$a$ and $b$')
  })

  it('promotes a bare environment to display math', () => {
    const source = 'Then:\n\n\\begin{align}\nx &= 1\n\\end{align}\n\nwhich follows.'

    const normalized = normalizeMathDelimiters(source)

    expect(normalized).toContain('$$\n\\begin{align}')
    expect(normalized).toContain('\\end{align}\n$$')
  })

  it('never reads a TODO marker as math', () => {
    // Milkdown serializes `[TODO:` as `\[TODO:` because a bare bracket could open a
    // link, and the section index depends on that escaping. A greedy reading would also
    // swallow the rest of the document hunting a `\]` that is not coming.
    const source = '## Methods\n\n\\[TODO: describe the rig]\n\n## Results\n\nProse here.\n'

    expect(normalizeMathDelimiters(source)).toBe(source)
  })

  it('leaves an escaped dollar alone', () => {
    const normalized = normalizeMathDelimiters(String.raw`It cost \$5, which is \(x\) dollars.`)

    expect(normalized).toContain('\\$5')
    expect(normalized).toContain('$x$')
  })

  it('treats fenced code as verbatim', () => {
    const source = 'Prose \\(a\\).\n\n```latex\n\\(not math\\)\n```\n\nMore \\(b\\).\n'

    const normalized = normalizeMathDelimiters(source)

    expect(normalized).toContain('```latex\n\\(not math\\)\n```')
    expect(normalized).toContain('$a$')
    expect(normalized).toContain('$b$')
  })

  it('is idempotent', () => {
    // A body is normalized on load and again whenever the server sends a fresh one, so
    // twice has to equal once or the autosave would fight itself.
    const source = 'Inline \\(x^2\\) and \\[ \\frac{a}{b} \\] and $already$ and $$done$$.'

    const once = normalizeMathDelimiters(source)

    expect(normalizeMathDelimiters(once)).toBe(once)
  })

  it('returns text with no backslashes untouched', () => {
    const source = 'Plain prose with $x$ and $$y$$ and nothing to fix.'

    expect(normalizeMathDelimiters(source)).toBe(source)
  })

  it('repairs prose accidentally serialized as an indented code block', () => {
    const first =
      '&#x20;   Linear transformations preserve addition and scalar multiplication while changing the direction and magnitude of ordinary vectors throughout the transformed space.'
    const second =
      '    Eigenvectors are the exceptional directions that stay aligned with themselves while the matrix changes only their magnitude through a corresponding eigenvalue.'

    const normalized = normalizeMathDelimiters(`${first}\n\n${second}`)

    expect(normalized).toContain('Linear transformations preserve addition')
    expect(normalized).toContain('\n\nEigenvectors are the exceptional directions')
    expect(normalized).not.toContain('&#x20;')
    expect(normalized).not.toContain('\n\n    Eigenvectors')
  })

  it('wraps undelimited LaTeX-bearing tokens without disturbing existing math', () => {
    const source = String.raw`The basis uses X_1, X_2, \dots, X_n in R^n, while $A$ stays delimited.`

    const normalized = normalizeMathDelimiters(source)

    expect(normalized).toContain('$X_1$, $X_2$, $\\dots$, $X_n$')
    expect(normalized).toContain('$R^n$')
    expect(normalizeMathDelimiters(String.raw`Use λ_1 and \frac{a}{b}.`)).toBe(
      'Use $λ_1$ and $\\frac{a}{b}$.',
    )
    expect(normalized).toContain('while $A$ stays delimited')
    expect(normalizeMathDelimiters(normalized)).toBe(normalized)
  })

  it('restores editor-escaped subscripts without treating identifiers as math', () => {
    const source = String.raw`Use X\_1 and λ\_1, but keep snake_case as prose.`

    expect(normalizeMathDelimiters(source)).toBe(
      'Use $X_1$ and $λ_1$, but keep snake_case as prose.',
    )
  })

  it('preserves math-looking tokens already inside inline and display math', () => {
    const source = 'Existing $X_1$ and $$R^n + X_2$$ stay exactly as written.'

    expect(normalizeMathDelimiters(source)).toBe(source)
  })

  it('does not repair indented prose or math-looking tokens inside a fence', () => {
    const source = '```text\n    X_1, X_2, \\dots, X_n remain literal here.\n```\n'

    expect(normalizeMathDelimiters(source)).toBe(source)
  })

  it('preserves a short indented code example', () => {
    const source = 'Example:\n    X_1 = transform(vector)\n'

    expect(normalizeMathDelimiters(source)).toBe(source)
  })
})

describe('idempotence over display math', () => {
  it('does not wrap an environment that is already display math', () => {
    // The case that actually bit: bodies are normalized server-side where AI text lands
    // and client-side on load, so a rule that re-wraps its own output nests one more
    // `$$` pair on every visit. The editor showed the result as an empty code block with
    // the equation spilled below it as literal prose.
    const source =
      'A bare environment:\n\n$$\n\\begin{align}\nu &= 1 \\\\\nv &= 2\n\\end{align}\n$$\n'

    expect(normalizeMathDelimiters(source)).toBe(source)
  })

  it('leaves delimiters inside display math alone', () => {
    const source = '$$\n\\begin{cases} a \\\\ b \\end{cases}\n$$\n'

    expect(normalizeMathDelimiters(source)).toBe(source)
  })

  it('still promotes an environment outside display math', () => {
    const source = 'Before.\n\n\\begin{align}\nx &= 1\n\\end{align}\n\nAfter.\n'

    const normalized = normalizeMathDelimiters(source)

    expect(normalized).toContain('$$\n\\begin{align}')
    expect(normalizeMathDelimiters(normalized)).toBe(normalized)
  })
})
