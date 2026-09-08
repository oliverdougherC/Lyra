'use client'

import { memo, useLayoutEffect, useMemo, useRef, type ComponentProps } from 'react'
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
 * Resolve a text offset inside `root` to a (node, offset) the range API accepts.
 * Offsets beyond the end of the static text (the settled render is not byte-identical to
 * the streaming one) clamp to the last node, which keeps a restore from throwing rather
 * than dropping a selection the reader actually held.
 */
function resolveOffset(root: HTMLElement, offset: number): { node: Node; offset: number } {
  let remaining = offset
  let lastText: Node | null = null
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  let node = walker.nextNode()
  while (node) {
    lastText = node
    const length = node.textContent?.length ?? 0
    if (remaining <= length) return { node, offset: remaining }
    remaining -= length
    node = walker.nextNode()
  }
  if (lastText !== null) {
    return { node: lastText, offset: (lastText.textContent ?? '').length }
  }
  return { node: root, offset: 0 }
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
  selectionRestore = null,
}: {
  content: string
  streaming?: boolean
  /** True once the stream finished; completion is only reported after this. */
  turnEnded?: boolean
  /**
   * Drains of the reveal queue, with the generation the drain belongs to. The caller
   * checks it: a drain from a replaced generation (a reset that cleared the answer, a
   * retry) is stale and must not finalize the turn the new generation is running.
   */
  onRevealComplete?: (generation?: string) => void
  /**
   * Identity of the current generation of this message. When it changes the reveal
   * schedule is cleared, so a regenerated answer does not inherit the slots of the
   * answer it replaces. The message lifecycle that owns regeneration (PLA-501) supplies
   * it — stable per message generation, e.g. the message id plus its attempt.
   */
  generation?: string
  /**
   * The selection the reader held in the live answer, as text offsets: the settled row
   * re-renders the same content through the static pipeline, which replaces every inner
   * node, and a live selection anchored in those nodes vanishes with them. Restored
   * once, after the static render has landed.
   */
  selectionRestore?: { anchor: number; focus: number } | null
}) {
  const renderContent = normalizeMarkdownForRender(content, streaming)
  const rootRef = useRevealCascade({
    content,
    enabled: streaming,
    settled: turnEnded,
    onDrained: onRevealComplete,
    generation,
  })

  // The settled swap is the moment the selection goes away: the reveal layer's own nodes
  // are gone, and the static markdown's are fresh. Restore once, after that render has
  // laid out — a layout effect, so no frame shows the answer without its selection.
  const restoredRef = useRef(false)
  useLayoutEffect(() => {
    if (streaming || selectionRestore === null || restoredRef.current) return
    const root = rootRef.current
    if (!root) return
    restoredRef.current = true
    const range = document.createRange()
    const start = resolveOffset(root, selectionRestore.anchor)
    const end = resolveOffset(root, selectionRestore.focus)
    range.setStart(start.node, start.offset)
    range.setEnd(end.node, end.offset)
    const selection = window.getSelection()
    if (!selection) return
    selection.removeAllRanges()
    selection.addRange(range)
  }, [streaming, selectionRestore, rootRef])

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
