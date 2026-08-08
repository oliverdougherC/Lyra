import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

/**
 * Ex Libris rules as lint (design system section 4, migration Stage 0.6):
 *
 *   - No em dashes, anywhere, in any interface copy, ever. The prototype build fails if one
 *     appears; the real build should too.
 *   - No italics in the interface. Italics are reserved for mathematics (KaTeX renders its
 *     own; the EB Garamond italic instance exists only as a fallback there). Emphasis in the
 *     interface changes size or weight, never slant.
 *
 * Both scan source with comments stripped, so a `—` inside an explanatory comment (this file
 * included) or a JSDoc dash does not trip the gate; only shipped strings and class lists do.
 */

const SRC = join(__dirname, '..', 'src')

function sourceFiles(dir: string): string[] {
  const out: string[] = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      out.push(...sourceFiles(full))
    } else if (/\.(ts|tsx)$/.test(entry)) {
      out.push(full)
    }
  }
  return out
}

/** Remove block and line comments so guidance about the rules is not mistaken for a breach. */
function stripComments(code: string): string {
  return code.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

const FILES = sourceFiles(SRC)

describe('no em dashes in the interface', () => {
  it('every source file is free of U+2014 outside comments', () => {
    const offenders: string[] = []
    for (const file of FILES) {
      const stripped = stripComments(readFileSync(file, 'utf8'))
      if (stripped.includes('—')) {
        const rel = file.slice(SRC.length + 1)
        const line = stripped.split('\n').findIndex((l) => l.includes('—')) + 1
        offenders.push(`${rel}:${line}`)
      }
    }
    expect(offenders, `use " · " or restructure; em dashes are banned:\n${offenders.join('\n')}`)
      .toEqual([])
  })
})

describe('no interface italics', () => {
  it('no className carries the italic utility', () => {
    // The Tailwind `italic` token inside a className value, but not `not-italic` and not the
    // word appearing in prose or a font-loader style array.
    const italicInClass = /className=(?:"|'|`)[^"'`]*(?<![-\w])italic\b/
    const offenders: string[] = []
    for (const file of FILES) {
      const stripped = stripComments(readFileSync(file, 'utf8'))
      if (italicInClass.test(stripped)) {
        offenders.push(file.slice(SRC.length + 1))
      }
    }
    expect(offenders, `emphasis is size or weight, never slant:\n${offenders.join('\n')}`).toEqual(
      [],
    )
  })
})
