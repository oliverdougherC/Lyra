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
  generation,
}: {
  content: string
  streaming?: boolean
  /** True once the stream finished; completion is only reported after this. */
  turnEnded?: boolean
  onRevealComplete?: () => void
  /**
   * Identity of the current generation of this message. When it changes the reveal
   * schedule is cleared, so a regenerated answer does not inherit the slots of the
   * answer it replaces. The message lifecycle that owns regeneration (PLA-501) supplies
   * it — stable per message generation, e.g. the message id plus its attempt.
   */
  generation?: string
}) {
  const renderContent = normalizeMarkdownForRender(content, streaming)
  const rootRef = useRevealCascade({
    content,
    enabled: streaming,
    settled: turnEnded,
    onDrained: onRevealComplete,
    generation,
  })
  const components = useMemo(() => ({ table: tableComponent }), [])
  // The reveal plugin reads the raw source (as plugin options) to anchor word identity,
  // and runs last: the equations it marks are already typeset.
  const rehypePlugins = useMemo(
    () =>
      (streaming
        ? [...REHYPE_PLUGINS, [rehypeRevealUnits, { rawSource: content }]]
        : REHYPE_PLUGINS) as NonNullable<MarkdownProps['rehypePlugins']>,
    [content, streaming],
  )

  return (
    <div ref={rootRef} className="assistant-content font-ai-response">
      <Markdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={rehypePlugins}
        components={components}
      >
        {renderContent}
      </Markdown>
    </div>
  )
})
