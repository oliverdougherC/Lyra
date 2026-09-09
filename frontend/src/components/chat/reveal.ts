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
 * 4. **The queue runs in reading order, and a source range that is on screen stays on
 *    screen.** A unit's deadline never lands before the unit ahead of it, no matter how
 *    the words are re-anchored on a re-render. A word's identity is the offset of its core
 *    in the raw source — stable for as long as the source grows at the end, which is all a
 *    stream does, and stable across the delimiters around it opening or closing. When a
 *    re-parse re-forms the words (an emphasis closes around them, a link or a code fence
 *    resolves), a unit whose source range sits inside a range that was already revealed —
 *    or already scheduled — inherits that range's deadline instead of starting a fresh
 *    reveal: what the reader already saw never fades again.
 *
 * A message may be *re-generated* (a retry replaces its answer in place). The caller then
 * passes a different `generation`, and the schedule is cleared: a retry's words must not
 * inherit the slots of the answer they replace. The contract for the lifecycle that owns
 * retries (PLA-501) is to hand each message generation a stable id — the message id plus
 * its attempt — so that in-place regeneration clears the schedule exactly once, and no
 * other re-render does.
 */

import { useEffect, useLayoutEffect, useRef, type RefObject } from 'react'

import { chatWork } from '@/components/chat/work-counters'

/** Marks one unit of the cascade. Its value is the key the schedule is remembered under. */
export const REVEAL_ATTRIBUTE = 'data-stream-word'

/**
 * Marks the raw-source range a unit covers, `start:end` as offsets into the normalized
 * source. The scheduler reads it to hand a re-formed unit the visibility of the range it
 * sits inside.
 */
export const REVEAL_SRC_ATTRIBUTE = 'data-stream-src'

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
  position?: { start?: { offset?: number }; end?: { offset?: number } }
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
    let equationIndex = 0
    let itemIndex = 0
    let codeIndex = 0
    let tableIndex = 0
    let ruleIndex = 0
    let controlIndex = 0

    const splitText = (node: RenderNode, children: RenderNode[]): void => {
      const value = node.value as string
      const base = node.position?.start?.offset
      let cursor = base ?? rawPointer
      for (const part of value.split(/(\s+)/)) {
        if (part && !/^\s+$/.test(part)) {
          // The word's core, found at or after the running pointer, is its identity: a
          // monotone walk over a source that only grows. The core — not the whole word —
          // is what is searched, because the markdown delimiters around a word appear and
          // disappear as emphasis opens and closes, and a word must not lose its slot
          // because of them.
          const core = coreOf(part)
          const at = core !== '' ? rawSource.indexOf(core, rawPointer) : -1
          const start = cursor
          cursor += part.length
          const key =
            at !== -1
              ? `stream-${at}`
              : // A part with no searchable core (pure punctuation) keys on the range it
                // covers: a per-frame counter would re-use the same key for different
                // parts on different frames and hand one of them the other's deadline.
                `streampos-${start}:${cursor}`
          if (at !== -1) rawPointer = at + core.length
          children.push({
            type: 'element',
            tagName: 'span',
            properties: {
              [REVEAL_ATTRIBUTE]: key,
              [REVEAL_SRC_ATTRIBUTE]: `${start}:${cursor}`,
            },
            children: [{ type: 'text', value: part }],
          })
        } else if (part) {
          cursor += part.length
          children.push({ type: 'text', value: part })
        }
      }
      // The node's own span in the source ends where the source says it ends, whether or
      // not the rendered text is the same length (entities decode shorter). Advancing the
      // pointer to the node's end keeps later words from anchoring inside it.
      const end = node.position?.end?.offset
      if (end !== undefined) rawPointer = Math.max(rawPointer, end)
    }

    /** The TeX a typeset equation was built from, kept in its MathML annotation. */
    const annotationText = (node: RenderNode): string | null => {
      for (const child of node.children ?? []) {
        if (child.type === 'element' && child.tagName === 'annotation') {
          let text = ''
          const collect = (n: RenderNode): void => {
            if (n.type === 'text' && n.value) text += n.value
            else if (n.children) n.children.forEach(collect)
          }
          child.children?.forEach(collect)
          return text
        }
        const found = annotationText(child)
        if (found !== null) return found
      }
      return null
    }

    /**
     * A katex element carries no source position: advance the pointer past its TeX,
     * found in the raw source from where the walk left off. Without this, a later word
     * that repeats a symbol of the equation would anchor to the typeset copy instead of
     * to itself.
     */
    const advancePastEquation = (node: RenderNode): void => {
      const tex = annotationText(node)
      if (tex) {
        const at = rawSource.indexOf(tex, rawPointer)
        if (at !== -1) rawPointer = at + tex.length
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
          advancePastEquation(node)
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
        // An element's source range covers what it rendered from — a code fence's rows,
        // a table's cells, and, for a link, the destination text after the label, which
        // never renders as words. Advancing past the end keeps a later word that repeats
        // text inside those ranges from anchoring to a hidden earlier occurrence.
        const end = child.type === 'element' ? child.position?.end?.offset : undefined
        if (end !== undefined) rawPointer = Math.max(rawPointer, end)
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
  /**
   * Called once the last scheduled unit has finished fading in, with the generation the
   * drain belongs to. The caller must check it: a drain from a replaced generation (a
   * reset, a retry) is stale and must not settle the turn the new one is running.
   */
  onDrained?: (generation: string | undefined) => void
}

/**
 * Update-work counts, read by the performance gate that keeps a commit from doing
 * unbounded work on a long answer. Zeroed per commit, not per message: a number is only
 * meaningful against the commit that produced it.
 *
 * The split that matters when bounding the work: `inheritanceScans` is the containment
 * checks the deadline-inheritance pass ran (one per historical range examined), while
 * `newUnitsScheduled` is how many units actually needed a fresh slot that commit. A pass
 * that costs O(new units × historical ranges) will show scans growing with BOTH numbers;
 * a bounded pass keeps scans proportional to the units it actually places.
 */
export const revealWork = {
  /** `reveal()` style writes this commit (the rest of the schedule work is reads). */
  styleWrites: 0,
  /** Containment checks the deadline inheritance ran this commit. */
  inheritanceScans: 0,
  /** Units this commit assigned a fresh slot to (not a remembered deadline). */
  newUnitsScheduled: 0,
  /** Units this commit took their deadline from a held range. */
  inheritedUnits: 0,
  /** Historical ranges the pass had to look through this commit. */
  historicalRanges: 0,
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
  // The source range each key covered the last time it was seen, so a unit re-formed by a
  // re-parse inherits the visibility of a range that is already on screen instead of
  // starting a fresh reveal.
  const rangesRef = useRef<Map<string, [number, number]>>(new Map())
  // The last deadline assigned to each key, kept after the key leaves the screen so an
  // inherited moment can be read even when the containing unit has merged away.
  const lastDeadlineRef = useRef<Map<string, number>>(new Map())
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
  // The generation is captured when the timer is ARMED, not read when it fires: the drain
  // it reports belongs to the queue that was on screen at arm time, whatever the ref
  // holds by the time the clock runs out. The queue itself moves under a settled flag —
  // a terminal result can replace the text with a longer one, or extend it, in the same
  // generation — so a content change re-arms the wait out of the new tail, and the report
  // still carries the generation the queue was armed under.
  useEffect(() => {
    if (!settled) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      onDrainedRef.current?.(generationRef.current)
      return
    }
    const drainGeneration = generationRef.current
    const remaining = Math.max(0, nextRevealAtRef.current + SETTLE_GRACE_MS - performance.now())
    const timer = window.setTimeout(() => onDrainedRef.current?.(drainGeneration), remaining)
    return () => window.clearTimeout(timer)
    // A generation change re-arms the report: the replacement answer has its own queue
    // to finish, and a drain of the old one has already fired (or been cancelled) —
    // settling must wait for the new generation's tail, reported under its identity.
    // A content change under an unchanged flag and generation is the same contract: the
    // accepted final text grew, and the wait must run out of the new tail, not the old
    // one that already drained.
  }, [settled, generation, content])

  useLayoutEffect(() => {
    // Reconcile the generation BEFORE anything else, including the early returns: an empty
    // replacement is a new generation too, and its drain must report the new identity —
    // reporting the old one would make the pane reject the drain the replacement owes and
    // leave the turn stuck.
    const generationChanged = generation !== undefined && generationRef.current !== generation
    if (generation !== undefined) generationRef.current = generation
    if (!enabled) {
      onDrainedRef.current?.(generationRef.current)
      return
    }

    if (content.length === 0) {
      scheduleRef.current.clear()
      rangesRef.current.clear()
      lastDeadlineRef.current.clear()
      nextRevealAtRef.current = 0
      onDrainedRef.current?.(generationRef.current)
      return
    }

    if (generationChanged) {
      // A new generation of this message: the slots on the books belong to the answer it
      // replaces, and every node of this one would reveal at once.
      scheduleRef.current.clear()
      rangesRef.current.clear()
      lastDeadlineRef.current.clear()
      nextRevealAtRef.current = 0
    }

    revealWork.styleWrites = 0
    revealWork.inheritanceScans = 0
    revealWork.newUnitsScheduled = 0
    revealWork.inheritedUnits = 0
    const now = performance.now()
    const nodes = Array.from(
      rootRef.current?.querySelectorAll<HTMLElement>(`[${REVEAL_ATTRIBUTE}]`) ?? [],
    )
    chatWork.revealNodeVisits += nodes.length
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

    // A per-commit snapshot of the historical ranges, pre-shaped for the inheritance
    // pass below: the pass reads it many times (once per unremembered unit) and writes
    // to `rangesRef` as it goes, so the snapshot is what keeps the inheritance reading the
    // state this commit started from. Sorting by source start (and carrying a prefix max
    // of the ends) is what bounds the pass: a range can contain the unit only if it starts
    // at or before the unit, and the moment every earlier range ends before the unit,
    // the walk stops — the rest of the history cannot hold the unit no matter how far it
    // stretches.
    const historicalRanges = [...rangesRef.current]
      .map(([key, range]) => ({ key, start: range[0], end: range[1] }))
      .sort((a, b) => a.start - b.start)
    const prefixMaxEnd: number[] = new Array(historicalRanges.length)
    for (let j = 0; j < historicalRanges.length; j += 1) {
      prefixMaxEnd[j] =
        j === 0
          ? historicalRanges[j]!.end
          : Math.max(historicalRanges[j]!.end, prefixMaxEnd[j - 1]!)
    }
    revealWork.historicalRanges = historicalRanges.length

    /**
     * The moment the unit's source range already holds: the deadline of the smallest
     * previously-seen range that contains it. A range on screen carries a past deadline,
     * which the unit re-uses exactly — it never re-hides; a pending range carries its
     * queued slot, so the unit joins the queue where its range sits.
     *
     * Bounded: candidates must start at or before the unit, so the walk begins at the last
     * such range and steps toward earlier starts, and it stops the moment the prefix max
     * of everything earlier is below the unit's end — no remaining range can contain it.
     * On a long answer with its own word ranges that is a handful of neighbors, not the
     * whole history.
     */
    const inheritedDeadline = (range: [number, number], selfKey: string): number | undefined => {
      let best: { span: number; deadline: number | undefined } | null = null
      let lo = 0
      let hi = historicalRanges.length
      while (lo < hi) {
        const mid = (lo + hi) >> 1
        if (historicalRanges[mid]!.start <= range[0]) lo = mid + 1
        else hi = mid
      }
      for (let j = lo - 1; j >= 0; j -= 1) {
        revealWork.inheritanceScans += 1
        const other = historicalRanges[j]!
        if (other.key !== selfKey && other.end >= range[1]) {
          const span = other.end - other.start
          if (best === null || span < best.span) {
            best = {
              span,
              deadline:
                scheduleRef.current.get(other.key) ?? lastDeadlineRef.current.get(other.key),
            }
            // Anything starting before this floor cannot hold a smaller span than `best`
            // over the same end: its span would exceed `best`'s, so it is skipped. Earlier ranges above that floor must still be examined;
            // jumping below it would skip potentially smaller containing ranges.
            const floorStart = range[1] - best.span
            if (j > 0 && historicalRanges[j - 1]!.start < floorStart) break
          }
        }
        // The whole earlier history ends before the unit's end: no range among it can
        // contain the unit (with or without a candidate on hand), so the walk is done.
        if (j === 0 || prefixMaxEnd[j - 1]! < range[1]) break
      }
      return best?.deadline
    }

    // Pass one: the content units, in reading order.
    let prev = now
    let assigned = false
    const assignedUnits: { node: HTMLElement; key: string; deadline: number }[] = []
    for (const node of units) {
      const key = node.dataset.streamWord
      if (!key) continue
      const src = node.dataset.streamSrc
      const range: [number, number] | null =
        src !== undefined && src.length > 2
          ? ((): [number, number] | null => {
              const sep = src.indexOf(':')
              return sep > 0 && sep < src.length - 1
                ? [Number(src.slice(0, sep)), Number(src.slice(sep + 1))]
                : null
            })()
          : null
      if (range !== null && Number.isFinite(range[0]) && Number.isFinite(range[1])) {
        // A re-parse can shrink the range a key covers — `a**b*c` becomes the bare `a`
        // when the emphasis resolves — but the reader saw the wider extent, and that
        // is the evidence later splits inherit from. Keep the union of what the key
        // has covered.
        const seen = rangesRef.current.get(key)
        rangesRef.current.set(
          key,
          seen === undefined ? range : [Math.min(seen[0], range[0]), Math.max(seen[1], range[1])],
        )
      }
      const remembered = scheduleRef.current.get(key)
      let deadline: number
      if (remembered !== undefined && remembered <= now) {
        // Already started: keep its exact moment, even if the node was replaced.
        deadline = remembered
      } else {
        // A pending deadline may move earlier — that is how the queue drains — and the
        // reading-order floor keeps the queue nondecreasing. A brand-new unit inside a
        // range the reader already holds takes that range's moment instead of the queue
        // tail: visible ranges keep their exact past deadline, so a re-formed word never
        // fades out to fade back in.
        const fresh = assigned ? prev + interval : now
        const inherited =
          remembered === undefined && range !== null ? inheritedDeadline(range, key) : undefined
        if (inherited !== undefined) {
          deadline = inherited <= now ? inherited : Math.max(inherited, prev)
          revealWork.inheritedUnits += 1
        } else {
          deadline = remembered !== undefined ? Math.max(Math.min(remembered, fresh), prev) : fresh
        }
        if (remembered === undefined) revealWork.newUnitsScheduled += 1
        assigned = true
      }
      assignedUnits.push({ node, key, deadline })
      if (deadline > prev) prev = deadline
    }

    // Reading order with past evidence: a unit that the reader already holds carries a
    // deadline in the past, and it stays put — re-hiding it to keep the queue flat would
    // be the very flash this cascade exists to prevent. When the unit ahead of it is
    // still pending, that unit moves earlier instead, which its queue always allows.
    for (let i = assignedUnits.length - 1; i > 0; i -= 1) {
      const earlier = assignedUnits[i - 1]
      const later = assignedUnits[i]
      if (earlier.deadline > later.deadline && earlier.deadline > now) {
        earlier.deadline = later.deadline
      }
    }

    for (const { node, key, deadline } of assignedUnits) {
      scheduleRef.current.set(key, deadline)
      reveal(node, deadline, now)
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

    // Remember where each key's deadline ended up, so a range the reader holds can be
    // read back on a later frame even after the unit that covered it merged away.
    for (const [key, deadline] of scheduleRef.current) {
      lastDeadlineRef.current.set(key, deadline)
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
  const delay = `${scheduledAt - start}ms`
  // A unit whose animation already ran carries a fixed delay: every later commit
  // computes the same value, and rewriting it is pure write traffic — the schedule
  // of a long, finished answer must stay quiet while a new tail arrives.
  if (
    node.classList.contains(REVEAL_VISIBLE_CLASS) &&
    node.style.getPropertyValue('--stream-word-delay') === delay
  ) {
    return
  }
  revealWork.styleWrites += 1
  node.style.setProperty('--stream-word-delay', delay)
  node.dataset.streamRevealAt = String(scheduledAt)
  node.classList.add(REVEAL_VISIBLE_CLASS)
}
