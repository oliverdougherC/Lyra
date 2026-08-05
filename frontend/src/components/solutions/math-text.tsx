'use client'

import { memo, type ComponentProps, type CSSProperties, type ReactNode } from 'react'
import Markdown from 'react-markdown'

import { normalizeMarkdownForRender } from '@/components/chat/markdown-utils'
import { KATEX_REHYPE_PLUGINS, REMARK_PLUGINS } from '@/components/chat/typeset'
import { cn } from '@/lib/utils'

type MarkdownProps = ComponentProps<typeof Markdown>

/** Paragraphs collapse to fragments so an inline render can live inside a flex row. */
const INLINE_COMPONENTS: MarkdownProps['components'] = {
  p: ({ children }: { children?: ReactNode }) => <>{children}</>,
}

/**
 * A problem statement, typeset.
 *
 * The solver's own answers have gone through the chat renderer since Phase 1, but the
 * problems it read out of the sheet were printed raw, so the screen where a student
 * checks Lyra's reading against their homework was the one screen showing
 * `x(t) = e-2tu(t -3)` where the sheet shows an exponent. That is the wrong way round:
 * the gate is worth least when its contents are hardest to read.
 *
 * Separate from `StreamingMarkdown` rather than a flag on it. That component carries the
 * chat's reveal cascade, its prose spacing, and its response typeface, none of which
 * belong on a two-line preview inside a list row.
 */
export const MathText = memo(function MathText({
  children,
  className,
  style,
  inline = false,
}: {
  children: string
  className?: string
  /** For the line clamp on a preview, which has no Tailwind equivalent here. */
  style?: CSSProperties
  /** Render without block wrappers, for a single-line row. */
  inline?: boolean
}) {
  return (
    <div className={cn('math-text', inline && 'math-text-inline', className)} style={style}>
      <Markdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={KATEX_REHYPE_PLUGINS}
        components={inline ? INLINE_COMPONENTS : undefined}
      >
        {normalizeMarkdownForRender(children, false, { promoteInlineMath: false })}
      </Markdown>
    </div>
  )
})
