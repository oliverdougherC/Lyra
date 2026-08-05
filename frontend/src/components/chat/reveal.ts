'use client'

/**
 * The reveal cascade: text arriving a word at a time rather than a paragraph at a time.
 *
 * Kept apart from any one renderer because every surface that shows written work wants
 * it, and the rules below are easy to get subtly wrong in a way that only shows up as
 * "the equations appear before the words".
 *
 * The rules this module exists to hold:
 *
 * 1. **A typeset equation is one unit.** Splitting it would fade in half a fraction at a
 *    time; skipping it would paint it instantly while the sentence around it was still
 *    arriving.
 * 2. **Pacing is CSS animation delay, never a timer chain.** SSE frames land in network
 *    chunks, so a burst of words can arrive in one commit, and timers throttle in a hidden
 *    tab and would strand the reveal mid-answer.
 * 3. **A unit's scheduled moment survives a re-render.** Markdown is re-parsed on every
 *    frame, so the DOM node holding a word can be replaced while its reveal is still
 *    pending. Re-applying the class without its delay would jump it to the front of the
 *    queue, which is exactly the artifact this module is here to prevent.
 */

import { useEffect, useLayoutEffect, useRef, type RefObject } from 'react'

/** Marks one unit of the cascade. Its value is the key the schedule is remembered under. */
export const REVEAL_ATTRIBUTE = 'data-stream-word'

/** Set on a unit once it has been scheduled; the CSS animation hangs off this. */
export const REVEAL_VISIBLE_CLASS = 'stream-word-visible'

const REVEAL_RELAXED_MS = 55
const REVEAL_STEADY_MS = 38
const REVEAL_FAST_MS = 26
const STEADY_THRESHOLD = 24
const FAST_THRESHOLD = 64

/** Long enough after the last word lands that the reveal reads as finished, not cut off. */
const SETTLE_GRACE_MS = 220

/**
 * A burst cascades quickly and a trickle cascades gently, so the rhythm tracks arrival
 * rather than running at one speed regardless of how fast the answer is coming.
 */
function revealIntervalFor(batchLength: number): number {
  if (batchLength > FAST_THRESHOLD) return REVEAL_FAST_MS
  if (batchLength > STEADY_THRESHOLD) return REVEAL_STEADY_MS
  return REVEAL_RELAXED_MS
}

type RenderNode = {
  type?: string
  tagName?: string
  value?: string
  children?: RenderNode[]
  properties?: Record<string, unknown>
  position?: { start?: { offset?: number } }
}

function hasClass(node: RenderNode, name: string): boolean {
  const value = node.properties?.className
  return Array.isArray(value) ? value.includes(name) : value === name
}

/**
 * A rehype plugin that wraps every prose word, and every typeset equation, in a unit the
 * cascade can schedule.
 *
 * Must run after `rehype-katex`, so the equations it protects have already been built.
 */
export function rehypeRevealUnits() {
  return (tree: RenderNode) => {
    let fallbackOffset = 0
    // Equations are keyed by their order in the answer rather than by a source offset. A
    // streamed answer only ever grows, so the third equation stays the third equation,
    // while its source offset moves as the normalizer reshapes the text around it.
    let equationIndex = 0

    const visit = (node: RenderNode): void => {
      if (node.type === 'element') {
        const tagName = node.tagName?.toLowerCase()
        if (tagName === 'pre' || tagName === 'code' || hasClass(node, 'hljs')) {
          return
        }
        if (hasClass(node, 'katex-display') || hasClass(node, 'katex')) {
          node.properties = {
            ...node.properties,
            [REVEAL_ATTRIBUTE]: `equation-${equationIndex}`,
          }
          equationIndex += 1
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
            properties: { [REVEAL_ATTRIBUTE]: key },
            children: [{ type: 'text', value: part }],
          })
        })
      })
      node.children = children
    }

    visit(tree)
  }
}

type RevealOptions = {
  /** The source being revealed. Every change schedules whatever is newly on screen. */
  content: string
  /** False renders everything at once, with no units and no schedule. */
  enabled: boolean
  /** True once the source has stopped growing; completion is only reported after this. */
  settled?: boolean
  /** Called once the last scheduled unit has finished fading in. */
  onDrained?: () => void
}

/**
 * Schedules the reveal of whatever the renderer has just painted.
 *
 * Returns the ref to put on the element wrapping the rendered output. Everything below it
 * carrying `REVEAL_ATTRIBUTE` is scheduled once, in document order, and stays put.
 */
export function useRevealCascade({
  content,
  enabled,
  settled = false,
  onDrained,
}: RevealOptions): RefObject<HTMLDivElement | null> {
  const rootRef = useRef<HTMLDivElement>(null)
  // Key to the wall-clock moment its animation was scheduled to start, so a unit whose DOM
  // node is replaced mid-reveal keeps the slot it was given rather than jumping the queue.
  const scheduleRef = useRef<Map<string, number>>(new Map())
  // Wall-clock time the next unit's reveal is scheduled to start.
  const nextRevealAtRef = useRef(0)
  const onDrainedRef = useRef(onDrained)

  // Kept in a ref so a changing callback does not re-arm the schedule, which only ever
  // touches refs and the DOM.
  useEffect(() => {
    onDrainedRef.current = onDrained
  }, [onDrained])

  // The source can settle without changing (a stream's `done` frame arrives after its last
  // token), which never re-runs the layout effect. On the flip, wait out whatever is still
  // scheduled and report then, so a caller that settles on this waits for the last words.
  // A single timeout is safe: in a hidden tab it simply waits until the reader looks again.
  useEffect(() => {
    if (!settled) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      onDrainedRef.current?.()
      return
    }
    const remaining = Math.max(0, nextRevealAtRef.current + SETTLE_GRACE_MS - performance.now())
    const timer = window.setTimeout(() => onDrainedRef.current?.(), remaining)
    return () => window.clearTimeout(timer)
  }, [settled])

  useLayoutEffect(() => {
    if (!enabled) {
      onDrainedRef.current?.()
      return
    }

    if (content.length === 0) {
      scheduleRef.current.clear()
      nextRevealAtRef.current = 0
      onDrainedRef.current?.()
      return
    }

    const now = performance.now()
    const nodes = Array.from(
      rootRef.current?.querySelectorAll<HTMLElement>(`[${REVEAL_ATTRIBUTE}]`) ?? [],
    )
    const fresh: HTMLElement[] = []
    nodes.forEach((node) => {
      const key = node.dataset.streamWord
      if (!key) return
      const scheduledAt = scheduleRef.current.get(key)
      if (scheduledAt !== undefined) {
        // Already scheduled. Re-applying the delay matters when this is a new DOM node
        // standing in for one React replaced: without it the unit would appear at once,
        // ahead of every word still waiting in front of it.
        reveal(node, Math.max(0, scheduledAt - now))
        return
      }
      fresh.push(node)
    })

    if (fresh.length === 0) return

    const interval = revealIntervalFor(fresh.length)
    // A burst, or the gap left by a hidden tab, lands far ahead of the schedule: restart
    // the cascade from now rather than piling every word behind an unreachable delay.
    const start = Math.max(nextRevealAtRef.current, now)
    fresh.forEach((node, index) => {
      const scheduledAt = start + index * interval
      scheduleRef.current.set(node.dataset.streamWord as string, scheduledAt)
      reveal(node, Math.max(0, scheduledAt - now))
    })
    nextRevealAtRef.current = start + fresh.length * interval
  }, [content, enabled])

  return rootRef
}

function reveal(node: HTMLElement, delayMs: number): void {
  node.style.setProperty('--stream-word-delay', `${delayMs}ms`)
  node.classList.add(REVEAL_VISIBLE_CLASS)
}
