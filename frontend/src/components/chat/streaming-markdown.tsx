'use client'

import { memo, useMemo, type ComponentProps } from 'react'
import Markdown from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'

import { normalizeMarkdownForRender } from '@/components/chat/markdown-utils'
import { rehypeRevealUnits, useRevealCascade } from '@/components/chat/reveal'
import { KATEX_REHYPE_PLUGINS, REMARK_PLUGINS } from '@/components/chat/typeset'

type MarkdownProps = ComponentProps<typeof Markdown>
const REHYPE_PLUGINS: NonNullable<MarkdownProps['rehypePlugins']> = [
  ...KATEX_REHYPE_PLUGINS,
  rehypeHighlight,
]
// The reveal plugin runs last, so the equations it protects have already been typeset.
const STREAMING_REHYPE_PLUGINS: NonNullable<MarkdownProps['rehypePlugins']> = [
  ...REHYPE_PLUGINS,
  rehypeRevealUnits,
]

type TableComponentProps = ComponentProps<'table'>

function tableComponent({ children, ...props }: TableComponentProps) {
  return (
    <div className="my-3 overflow-x-auto rounded-md border border-border">
      <table {...props}>{children}</table>
    </div>
  )
}

/**
 * Memoized on `content` so a stream that appends one token does not re-parse the markdown
 * for every other message on screen.
 */
export const StreamingMarkdown = memo(function StreamingMarkdown({
  content,
  streaming = false,
  turnEnded = false,
  onRevealComplete,
}: {
  content: string
  streaming?: boolean
  /** True once the stream finished; completion is only reported after this. */
  turnEnded?: boolean
  onRevealComplete?: () => void
}) {
  const renderContent = normalizeMarkdownForRender(content, streaming)
  const rootRef = useRevealCascade({
    content,
    enabled: streaming,
    settled: turnEnded,
    onDrained: onRevealComplete,
  })
  const components = useMemo(() => ({ table: tableComponent }), [])

  return (
    <div ref={rootRef} className="assistant-content font-ai-response">
      <Markdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={streaming ? STREAMING_REHYPE_PLUGINS : REHYPE_PLUGINS}
        components={components}
      >
        {renderContent}
      </Markdown>
    </div>
  )
})
