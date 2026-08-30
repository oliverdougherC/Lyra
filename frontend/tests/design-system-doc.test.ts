import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

/**
 * The public design spec tracks the shipped typography (PLA-145 regression check).
 *
 * The one thing a system-wide design migration can silently leave behind is the public
 * design-system document. The migration to Ex Libris changed frontend/src/app/layout.tsx
 * while docs/design-system.md kept describing the faces it had retired, and the drift
 * survived until a Linear issue caught it. This test binds the two at the narrow seam
 * that catches exactly that kind of drift:
 *
 *   - Every font family the app loads (parsed from the next/font/google import in
 *     layout.tsx) must be named in the canonical spec.
 *   - Faces the Ex Libris migration retired must not appear in the spec, where their
 *     presence means the spec still describes the old system as current.
 *
 * The next font migration edits layout.tsx; this test fails until the document is
 * updated in the same change. That is the point. It deliberately does not restate the
 * tokens: scripts/check_contrast.py recomputes the color contract from globals.css, and
 * duplicating values here would create the second hard-coded source of truth it avoids.
 */

const ROOT = join(__dirname, '..')
const LAYOUT = join(ROOT, 'src', 'app', 'layout.tsx')
const SPEC = join(ROOT, '..', 'docs', 'design-system.md')

// next/font identifiers are font family names with underscores (EB_Garamond); the spec
// writes the family the way a reader meets it. An identifier missing from this table is
// asserted under its own name, so a new face cannot slip through silently.
const DISPLAY_NAMES: Record<string, string> = {
  Caveat: 'Caveat',
  Cinzel: 'Cinzel',
  EB_Garamond: 'EB Garamond',
  JetBrains_Mono: 'JetBrains Mono',
}

// Faces the Ex Libris migration retired. If one of these ever reappears here it was
// reloaded into the app: a new system, and this list is where that gets acknowledged.
const RETIRED_FACES = ['DM Sans', 'Fraunces', 'Source Serif']

function loadedFontIdentifiers(): string[] {
  const layout = readFileSync(LAYOUT, 'utf8')
  const match = /import\s*\{([^}]*)\}\s*from\s*'next\/font\/google'/.exec(layout)
  if (!match) {
    throw new Error('next/font/google import not found in src/app/layout.tsx')
  }
  return match[1]
    .split(',')
    .map((identifier) => identifier.trim())
    .filter(Boolean)
}

describe('design-system.md tracks the shipped typography', () => {
  const spec = readFileSync(SPEC, 'utf8')

  it('names every font family the app loads', () => {
    const displayNames = loadedFontIdentifiers().map(
      (identifier) => DISPLAY_NAMES[identifier] ?? identifier,
    )
    const missing = displayNames.filter((name) => !spec.includes(name))
    expect(missing, `docs/design-system.md does not name: ${missing.join(', ')}`).toEqual([])
  })

  it('does not describe retired faces as current', () => {
    const stale = RETIRED_FACES.filter((face) => spec.includes(face))
    expect(stale, `retired faces named in the spec: ${stale.join(', ')}`).toEqual([])
  })
})
