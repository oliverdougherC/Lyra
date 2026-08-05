'use client'

/**
 * How Lyra typesets mathematics, in one place.
 *
 * Two renderers read this: the chat's streaming surface and the solver's statement rows.
 * They differ in what they do around the mathematics, never in the mathematics itself, and
 * when they each held their own copy of this configuration they drifted.
 */

import type { ComponentProps } from 'react'
import type Markdown from 'react-markdown'
import rehypeKatex from 'rehype-katex'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'

type MarkdownProps = ComponentProps<typeof Markdown>

export const KATEX_OPTIONS = {
  // A statement transcribed from a PDF makes a stray brace a question of when rather than
  // whether. A failed equation renders in the danger colour and the rest of the line
  // survives; throwing would blank the row the student is meant to be reading.
  throwOnError: false,
  strict: 'ignore' as const,
  errorColor: 'var(--danger-text)',
}

export const REMARK_PLUGINS = [remarkGfm, remarkMath]

/** KaTeX only. Callers that also highlight code append their own plugin after this. */
export const KATEX_REHYPE_PLUGINS: NonNullable<MarkdownProps['rehypePlugins']> = [
  [rehypeKatex, KATEX_OPTIONS],
]
