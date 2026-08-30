'use client'

import { useEffect } from 'react'

/**
 * Global error boundary (Next.js App Router special file). This component replaces the
 * whole document when the root layout or template itself fails, so everything the normal
 * root layout provides is absent: globals.css, the loaded fonts, providers, and the
 * pre-paint theme script in <head>. The Next.js contract requires this file to render
 * its own <html> and <body> and to carry its own styles, so it depends on nothing but
 * React.
 *
 * The palette mirrors docs/exlibris-design-system.md (stone ground, paper sheet, ink,
 * the pen) for both themes. The theme is applied on mount rather than by a pre-paint
 * <head> script: in this flow the initial HTML is a minimal seed document and React
 * does not run inline scripts it hoists client-side, so an inline script would never
 * execute. THEME_STORAGE_KEY mirrors the key in @/lib/theme; tests/error-boundaries.
 * test.tsx seeds it through the canonical export so the two cannot drift silently.
 *
 * Privacy contract: the `error` prop is typed for the boundary contract only. Its
 * `message`, `digest`, stack, and any application state never reach the DOM, and
 * nothing here reports the error anywhere: Lyra keeps every byte of failure local.
 */

const THEME_STORAGE_KEY = 'lyra-theme'

const DARK_QUERY = '(prefers-color-scheme: dark)'

const GLOBAL_ERROR_STYLES = `
:root {
  color-scheme: light;
  --gb-bg: #e9e4d8;
  --gb-paper: #faf7ee;
  --gb-ink: #28231a;
  --gb-muted: #645c4c;
  --gb-line: #857a58;
  --gb-accent: #a9c3a0;
  --gb-accent-hover: #9bb891;
  --gb-accent-ink: #1d3324;
  --gb-hand: #2440c0;
  --gb-shadow: 0 12px 30px rgb(40 35 26 / 0.08);
}
.dark {
  color-scheme: dark;
  --gb-bg: #0f0d0a;
  --gb-paper: #171410;
  --gb-ink: #e7dfcf;
  --gb-muted: #9a917d;
  --gb-line: #6b6449;
  --gb-accent: #e6d69b;
  --gb-accent-hover: #efe1ae;
  --gb-accent-ink: #2a2410;
  --gb-hand: #a8b6f0;
  --gb-shadow: 0 12px 30px rgb(0 0 0 / 0.55);
}
*,
*::before,
*::after {
  box-sizing: border-box;
  margin: 0;
}
html,
body {
  height: 100%;
}
body {
  background: var(--gb-bg);
  color: var(--gb-ink);
  font-family: ui-serif, Georgia, 'Times New Roman', serif;
  font-size: 16px;
  line-height: 1.55;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.gb-card {
  width: 100%;
  max-width: 420px;
  background: var(--gb-paper);
  border: 1px solid var(--gb-line);
  border-radius: 8px;
  padding: 32px;
  text-align: center;
  box-shadow: var(--gb-shadow);
}
.gb-card h1 {
  font-size: 18px;
  font-weight: 600;
  letter-spacing: -0.01em;
}
.gb-card p {
  margin-top: 12px;
  font-size: 14px;
  line-height: 1.6;
  color: var(--gb-muted);
}
.gb-button {
  margin-top: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 40px;
  padding: 0 16px;
  background: var(--gb-accent);
  color: var(--gb-accent-ink);
  border: 0;
  border-radius: 8px;
  font-family: inherit;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
}
.gb-button:hover {
  background: var(--gb-accent-hover);
}
.gb-button:focus-visible {
  outline: 2px solid var(--gb-hand);
  outline-offset: 2px;
}
`

export default function GlobalError({
  error,
  retry,
}: {
  error: Error & { digest?: string }
  retry: () => void
}) {
  // `error` is deliberately unused: reading it is how a fallback would leak.
  void error

  useEffect(() => {
    let dark = false
    try {
      const stored = localStorage.getItem(THEME_STORAGE_KEY)
      dark = stored === 'dark' || (stored === 'system' && window.matchMedia(DARK_QUERY).matches)
    } catch {
      dark = false
    }
    document.documentElement.classList.toggle('dark', dark)
  }, [])

  return (
    // global-error must include html and body tags
    <html lang="en" suppressHydrationWarning>
      <head>
        <title>Lyra</title>
        <style>{GLOBAL_ERROR_STYLES}</style>
      </head>
      <body>
        <div role="alert" className="gb-card">
          <h1>Something went wrong</h1>
          <p>An unexpected error stopped Lyra from loading. Try again to reload.</p>
          <button type="button" className="gb-button" onClick={retry}>
            Try again
          </button>
        </div>
      </body>
    </html>
  )
}
