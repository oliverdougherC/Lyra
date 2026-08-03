'use client'

import { memo, useEffect, useLayoutEffect, useRef, type ComponentProps } from 'react'
import Markdown from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import rehypeKatex from 'rehype-katex'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'

import { normalizeMarkdownForRender } from '@/components/chat/markdown-utils'

type RenderNode = {
  type?: string
  tagName?: string
  value?: string
  children?: RenderNode[]
  properties?: Record<string, unknown>
  position?: { start?: { offset?: number } }
}

const REMARK_PLUGINS = [remarkGfm, remarkMath]
const KATEX_OPTIONS = {
  throwOnError: false,
  strict: 'ignore' as const,
  errorColor: 'var(--danger-text)',
}
type MarkdownProps = ComponentProps<typeof Markdown>
type RehypePlugins = NonNullable<MarkdownProps['rehypePlugins']>
const REHYPE_PLUGINS: RehypePlugins = [[rehypeKatex, KATEX_OPTIONS], rehypeHighlight]

// The cascade is paced by CSS animation delays, not a JS timer chain: the browser
// delivers SSE frames in network chunks, so words can land in one commit, and timers
// throttle in hidden tabs and stall the reveal mid-stream. Each word's delay is
// computed from a running wall-clock schedule so bursts cascade rhythmically while a
// steady stream keeps pace with arrival.
const REVEAL_RELAXED_MS = 55
const REVEAL_STEADY_MS = 38
const REVEAL_FAST_MS = 26
const STEADY_THRESHOLD = 24
const FAST_THRESHOLD = 64
const SETTLE_GRACE_MS = 220

function revealIntervalFor(batchLength: number): number {
  if (batchLength > FAST_THRESHOLD) return REVEAL_FAST_MS
  if (batchLength > STEADY_THRESHOLD) return REVEAL_STEADY_MS
  return REVEAL_RELAXED_MS
}

function hasClass(node: RenderNode, name: string): boolean {
  const value = node.properties?.className
  return Array.isArray(value) ? value.includes(name) : value === name
}
function rehypeStreamWords() {
  return (tree: RenderNode) => {
    let fallbackOffset = 0

    const visit = (node: RenderNode): void => {
      if (node.type === 'element') {
        const tagName = node.tagName?.toLowerCase()
        if (
          tagName === 'pre' ||
          tagName === 'code' ||
          hasClass(node, 'katex') ||
          hasClass(node, 'hljs')
        ) {
          return
        }
      }

      if (!node.children) return
      const children: RenderNode[] = []
      node.children.forEach((child) => {
        if (child.type !== 'text' || !child.value) {
          if (child.type === 'text' && child.value) fallbackOffset += child.value.length
          visit(child)
          children.push(child)
          return
        }

        const sourceOffset = child.position?.start?.offset ?? fallbackOffset
        const parts = child.value.split(/(\s+)/)
        let wordIndex = 0
        parts.forEach((part) => {
          fallbackOffset += part.length
          if (!part || /^\s+$/.test(part)) {
            children.push({ type: 'text', value: part })
            return
          }
          const key = `stream-${sourceOffset}-${wordIndex}`
          wordIndex += 1
          children.push({
            type: 'element',
            tagName: 'span',
            properties: { 'data-stream-word': key },
            children: [{ type: 'text', value: part }],
          })
        })
      })
      node.children = children
    }

    visit(tree)
  }
}

type TableComponentProps = ComponentProps<'table'>

function tableComponent({ children, ...props }: TableComponentProps) {
  return (
    <div className="my-3 overflow-x-auto rounded-md border border-border">
      <table {...props}>{children}</table>
    </div>
  )
}

function revealNow(node: HTMLElement): void {
  node.classList.add('stream-word-visible')
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
  const rootRef = useRef<HTMLDivElement>(null)
  const seenWordKeys = useRef<Set<string>>(new Set())
  // Wall-clock time the next word's reveal animation is scheduled to start.
  const nextRevealAtRef = useRef(0)
  const onRevealCompleteRef = useRef(onRevealComplete)
  const turnEndedRef = useRef(turnEnded)
  const renderContent = normalizeMarkdownForRender(content, streaming)

  // Keep the latest callback without re-arming timers, which only touch refs.
  useEffect(() => {
    onRevealCompleteRef.current = onRevealComplete
  }, [onRevealComplete])

  useEffect(() => {
    turnEndedRef.current = turnEnded
  }, [turnEnded])

  // The turn can end without a content change (the `done` frame arrives after the last
  // token), which never re-runs the layout effect. On the flip, compute how long the
  // remaining scheduled reveals will take and report completion after that, so the
  // optimistic turn settles only once the last words have faded in. A single timeout is
  // safe here: if the tab is hidden the settle simply waits until the user looks again.
  useEffect(() => {
    if (!turnEnded) return
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (reducedMotion) {
      onRevealCompleteRef.current?.()
      return
    }
    const remaining = Math.max(0, nextRevealAtRef.current + SETTLE_GRACE_MS - performance.now())
    const timer = window.setTimeout(() => onRevealCompleteRef.current?.(), remaining)
    return () => window.clearTimeout(timer)
  }, [turnEnded])

  useLayoutEffect(() => {
    if (!streaming) {
      onRevealCompleteRef.current?.()
      return
    }

    if (content.length === 0) {
      seenWordKeys.current.clear()
      nextRevealAtRef.current = 0
      onRevealCompleteRef.current?.()
      return
    }

    const nodes = Array.from(
      rootRef.current?.querySelectorAll<HTMLElement>('[data-stream-word]') ?? [],
    )
    const fresh: HTMLElement[] = []
    nodes.forEach((node) => {
      const key = node.dataset.streamWord
      if (!key) return
      if (seenWordKeys.current.has(key)) {
        revealNow(node)
        return
      }
      seenWordKeys.current.add(key)
      fresh.push(node)
    })

    if (fresh.length === 0) return

    const now = performance.now()
    const interval = revealIntervalFor(fresh.length)
    // A burst (or a hidden-tab gap) lands far ahead of the schedule: restart the cascade
    // from now instead of letting every word pile up behind an unreachable delay.
    const start = Math.max(nextRevealAtRef.current, now)
    fresh.forEach((node, index) => {
      node.style.setProperty(
        '--stream-word-delay',
        `${Math.max(0, start + index * interval - now)}ms`,
      )
      revealNow(node)
    })
    nextRevealAtRef.current = start + fresh.length * interval
  }, [content, streaming])

  return (
    <div ref={rootRef} className="assistant-content font-ai-response">
      <Markdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={streaming ? [...REHYPE_PLUGINS, rehypeStreamWords] : REHYPE_PLUGINS}
        components={{ table: tableComponent }}
      >
        {renderContent}
      </Markdown>
      {streaming ? (
        <span
          className="bg-accent-primary ml-0.5 inline-block h-4 w-[2px] translate-y-0.5 motion-safe:animate-pulse"
          aria-hidden
        />
      ) : null}
    </div>
  )
})
