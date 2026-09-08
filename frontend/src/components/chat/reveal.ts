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
 * 1. **Each visual thing is exactly one reveal layer.** A prose word is one unit; a typeset
 *    equation is one unit; a code block, a table, a rule, and a checkbox are each one unit;
 *    a list item is a marker unit whose words still reveal on their own layer. Splitting an
 *    equation would fade in half a fraction at a time; skipping code would paint it
 *    instantly while the sentence around it was still arriving; and an item that reveals
 *    as a whole fades its children in twice — once with the item, once with the words.
 * 1a. **A list marker arrives with its first word.** The bullet is drawn from
 *    `display: list-item`, not from any text the splitter could reach, so a list left to
 *    itself stamps its bullets down complete and the words then fall into place around
 *    them. The item carries a marker unit, and the scheduler gives it the deadline of the
 *    first content unit inside it — so the bullet rides in with the first word, never
 *    before the line it belongs to, never after.
 * 2. **Pacing is CSS animation delay, never a timer chain.** SSE frames land in network
 *    chunks, so a burst of words can arrive in one commit, and timers throttle in a hidden
 *    tab and would strand the reveal mid-answer.
 * 3. **A unit's scheduled moment survives a re-render.** Markdown is re-parsed on every
 *    frame, so the DOM node holding a word can be replaced while its reveal is still
 *    pending. Re-applying the class without its delay would jump it to the front of the
 *    queue, which is exactly the artifact this module is here to prevent.
 * 4. **The queue runs in reading order.** A unit's deadline never lands before the unit
 *    ahead of it, no matter how the words are re-anchored on a re-render. The schedule is
 *    reconciled so pending deadlines may move earlier, never later, and a word's identity
 *    is the offset of its core in the raw source it was read from — stable for as long as
 *    the source grows at the end, which is all a stream does, and stable across the
 *    delimiters around it opening or closing.
 *
 * A message may be *re-generated* (a retry replaces its answer in place). The caller then
 * passes a different `generation`, and the schedule is cleared: a retry's words must not
 * inherit the slots of the answer they replace. The contract for the lifecycle that owns
 * retries (PLA-501) is to hand each message generation a stable id — the message id plus
 * its attempt — so that in-place regeneration clears the schedule exactly once, and no
 * other re-render does.
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

/** Keep a fast model from leaving seconds of unread text queued behind the stream. */
const MAX_REVEAL_BACKLOG_MS = 500

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

function tag(node: RenderNode, key: string): void {
  node.properties = { ...node.properties, [REVEAL_ATTRIBUTE]: key }
}

/**
 * The letters and digits of a word, without the punctuation that surrounds it.
 *
 * Markdown delimiters are not in the rendered text: `**alpha` is a literal word while the
 * emphasis is open and `alpha` once it closes, and both are the same word. Anchoring on the
 * core keeps the word's identity — and its scheduled slot — across that re-parse.
 */
function coreOf(word: string): string {
  const isCore = (ch: string) => /[\p{L}\p{N}]/u.test(ch)
  let a = 0
  let b = word.length
  while (a < b && !isCore(word[a])) a += 1
  while (b > a && !isCore(word[b - 1])) b -= 1
  return word.slice(a, b)
}

/**
 * A rehype plugin that marks every reveal unit of the rendered answer.
 *
 * Words are keyed by the offset of their core in the raw source, so a word keeps its
 * identity — and its scheduled slot — across re-renders while the answer grows at the
 * end, and across the markdown delimiters around it opening or closing. Equations and the
 * other atomic visuals are keyed by their order in the answer, which is stable for the
 * same reason.
 *
 * Registered as `[rehypeRevealUnits, { rawSource }]` in the rehype plugin list, so the
 * source travels as plugin options. Run after `rehype-katex`, so the equations it marks
 * have already been built.
 */
export function rehypeRevealUnits(options?: { rawSource?: string }) {
  const rawSource = options?.rawSource ?? ''
  return (tree: RenderNode) => {
    // The raw source only grows at the end, so a monotone pointer finds each word at the
    // same offset it first arrived with.
    let rawPointer = 0
    let positional = 0
    let equationIndex = 0
    let itemIndex = 0
    let codeIndex = 0
    let tableIndex = 0
    let ruleIndex = 0
    let controlIndex = 0

    const splitText = (node: RenderNode, children: RenderNode[]): void => {
      const value = node.value as string
      for (const part of value.split(/(\s+)/)) {
        if (part && !/^\s+$/.test(part)) {
          // The word's core, found at or after the running pointer, is its identity: a
          // monotone walk over a source that only grows. The core — not the whole word —
          // is what is searched, because the markdown delimiters around a word appear and
          // disappear as emphasis opens and closes, and a word must not lose its slot
          // because of them.
          const core = coreOf(part)
          const at = core !== '' ? rawSource.indexOf(core, rawPointer) : -1
          const key = at !== -1 ? `stream-${at}` : `streampos-${positional++}`
          if (at !== -1) rawPointer = at + core.length
          children.push({
            type: 'element',
            tagName: 'span',
            properties: { [REVEAL_ATTRIBUTE]: key },
            children: [{ type: 'text', value: part }],
          })
        } else if (part) {
          children.push({ type: 'text', value: part })
        }
      }
    }

    const visit = (node: RenderNode): void => {
      if (node.type === 'element') {
        const tagName = node.tagName?.toLowerCase() ?? ''
        const className = node.properties?.className
        const classes = Array.isArray(className)
          ? className
          : typeof className === 'string'
            ? className.split(/\s+/)
            : []
        const has = (name: string) => classes.includes(name)
        if (tagName === 'pre' || tagName === 'code') {
          // One unit, and its contents are never split: code is copied byte-for-byte.
          tag(node, `code-${codeIndex++}`)
          return
        }
        if (tagName === 'table') {
          // A table is read in one pass; splitting its cells would interleave the rows.
          tag(node, `table-${tableIndex++}`)
          return
        }
        if (tagName === 'hr') {
          tag(node, `rule-${ruleIndex++}`)
          return
        }
        if (tagName === 'input' && node.properties?.type === 'checkbox') {
          // The checkbox is the visible head of its task-list item; it reveals with the
          // item's first word and its own label stays intact.
          tag(node, `control-${controlIndex++}`)
          return
        }
        if (has('katex')) {
          // A typeset equation is one unit: katex-display carries its katex child, so the
          // first match takes the whole equation.
          tag(node, `equation-${equationIndex++}`)
          return
        }
        if (tagName === 'li') {
          // A marker unit, not an opacity layer: its descendants still reveal below it,
          // and the scheduler gives it the deadline of its first content unit.
          tag(node, `item-${itemIndex++}`)
        }
      }

      if (!node.children) return
      const children: RenderNode[] = []
      node.children.forEach((child) => {
        if (child.type === 'text' && child.value) {
          splitText(child, children)
          return
        }
        visit(child)
        children.push(child)
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
  /**
   * Identity of the current generation of this message. A change clears the schedule, so
   * a regenerated answer does not inherit the slots of the one it replaces.
   */
  generation?: string
  /** Called once the last scheduled unit has finished fading in. */
  onDrained?: () => void
}

/**
 * Schedules the reveal of whatever the renderer has just painted.
 *
 * Returns the ref to put on the element wrapping the rendered output. Everything below it
 * carrying `REVEAL_ATTRIBUTE` is scheduled once, in reading order, and stays put.
 */
export function useRevealCascade({
  content,
  enabled,
  settled = false,
  generation,
  onDrained,
}: RevealOptions): RefObject<HTMLDivElement | null> {
  const rootRef = useRef<HTMLDivElement>(null)
  // Key to the wall-clock moment its animation was scheduled to start, so a unit whose DOM
  // node is replaced mid-reveal keeps the slot it was given rather than jumping the queue.
  const scheduleRef = useRef<Map<string, number>>(new Map())
  // Wall-clock time the last unit's reveal is scheduled to start.
  const nextRevealAtRef = useRef(0)
  const generationRef = useRef(generation)
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

    if (generation !== undefined && generationRef.current !== generation) {
      // A new generation of this message: the slots on the books belong to the answer it
      // replaces, and every node of this one would reveal at once.
      scheduleRef.current.clear()
      nextRevealAtRef.current = 0
    }
    if (generation !== undefined) generationRef.current = generation

    const now = performance.now()
    const nodes = Array.from(
      rootRef.current?.querySelectorAll<HTMLElement>(`[${REVEAL_ATTRIBUTE}]`) ?? [],
    )
    // List items are marker units: their content units reveal, and the marker rides the
    // first one. Everything else is content, in document reading order.
    const items = nodes.filter((node) => node.tagName === 'LI')
    const units = nodes.filter((node) => node.tagName !== 'LI')
    // Pending content only: a marker's slot comes from its content, so it adds no queue.
    const pending = units.filter((unit) => {
      const key = unit.dataset.streamWord
      return key && (scheduleRef.current.get(key) ?? Infinity) > now
    }).length
    const interval = Math.min(
      revealIntervalFor(pending),
      MAX_REVEAL_BACKLOG_MS / Math.max(1, pending),
    )

    // Pass one: the content units, in reading order. Deadlines come out nondecreasing, so
    // the cascade can never reveal a word before the one ahead of it.
    let prev = now
    let assigned = false
    for (const node of units) {
      const key = node.dataset.streamWord
      if (!key) continue
      const remembered = scheduleRef.current.get(key)
      let deadline: number
      if (remembered !== undefined && remembered <= now) {
        // Already started: keep its exact moment, even if the node was replaced.
        deadline = remembered
      } else {
        // A pending deadline may move earlier — that is how the queue drains — and the
        // reading-order floor keeps the queue nondecreasing.
        const fresh = assigned ? prev + interval : now
        deadline = remembered !== undefined ? Math.max(Math.min(remembered, fresh), prev) : fresh
        assigned = true
      }
      scheduleRef.current.set(key, deadline)
      reveal(node, deadline, now)
      if (deadline > prev) prev = deadline
    }

    // Pass two: each list marker takes the deadline of the first content unit inside it.
    // A nested list's inner marker waits for its own first word, so a bullet never leads
    // the line it labels.
    for (const item of items) {
      const key = item.dataset.streamWord
      if (!key) continue
      const first = item.querySelector<HTMLElement>(`[${REVEAL_ATTRIBUTE}]:not(li)`)
      const contentDeadline = first
        ? scheduleRef.current.get(first.dataset.streamWord ?? '')
        : undefined
      const deadline = contentDeadline ?? scheduleRef.current.get(key) ?? prev
      scheduleRef.current.set(key, deadline)
      reveal(item, deadline, now)
    }

    nextRevealAtRef.current = Math.max(
      now,
      ...nodes.map((node) => scheduleRef.current.get(node.dataset.streamWord ?? '') ?? now),
    )
  }, [content, enabled, generation])

  return rootRef
}

// CSS animation delays are relative to when the animation was attached, not to
// the most recent token. Rewriting a remaining delay every render shifts its clock.
const animationStarts = new WeakMap<HTMLElement, number>()

function reveal(node: HTMLElement, scheduledAt: number, now: number): void {
  let start = animationStarts.get(node)
  if (start === undefined || !node.classList.contains(REVEAL_VISIBLE_CLASS)) {
    start = now
    animationStarts.set(node, start)
  }
  // A negative delay resumes a replacement node at the elapsed position, so old
  // list text cannot repeatedly fade from invisible as Markdown is reconstructed.
  node.style.setProperty('--stream-word-delay', `${scheduledAt - start}ms`)
  node.dataset.streamRevealAt = String(scheduledAt)
  node.classList.add(REVEAL_VISIBLE_CLASS)
}
