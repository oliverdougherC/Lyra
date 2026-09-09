'use client'

import {
  memo,
  useLayoutEffect,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentProps,
} from 'react'
import Markdown from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'

import { normalizeMarkdownForRender } from '@/components/chat/markdown-utils'
import { rehypeRevealUnits, useRevealCascade } from '@/components/chat/reveal'
import { chatWork } from '@/components/chat/work-counters'
import { KATEX_REHYPE_PLUGINS, REMARK_PLUGINS } from '@/components/chat/typeset'

type MarkdownProps = ComponentProps<typeof Markdown>
const REHYPE_PLUGINS: NonNullable<MarkdownProps['rehypePlugins']> = [
  ...KATEX_REHYPE_PLUGINS,
  rehypeHighlight,
]

/**
 * How long a long live answer may hold its last rendered document before a longer one
 * must be re-parsed (PLA-511).
 *
 * The whole document is what gets parsed — never a fragment — so lists, tables, code
 * fences, display math, reference links and the rest keep their parser context exactly
 * as the static pipeline gives them; the schedule only decides WHEN a re-parse runs.
 * 75 ms is about three steady reveal intervals (38 ms), so a re-parse lands before a
 * queue of three or more words can drain, and the reveal cascade's own pacing keeps the
 * reader from noticing the cadence. Below {@link STREAM_REPARSE_MIN_CHARS} the full
 * pipeline is sub-frame (measured in the streaming-cost report), so short answers parse
 * on every commit and keep the immediate fidelity the playback tests rely on.
 *
 * The gap is a FLOOR: it also scales with what a re-parse of the current document actually
 * costs (measured, see {@link REPARSE_WORK_FACTOR}). A document whose re-parse takes
 * longer than the floor cannot be re-parsed on the floor — the measured cost IS the
 * cadence — so the gap grows to keep the parse duty cycle bounded instead of letting a
 * slow document re-parse on every commit (the baseline behavior the streaming-cost
 * report shows at 40 k characters).
 */
export const STREAM_REPARSE_GAP_MS = 75
/** Below this length a whole-document re-parse is cheap enough to run on every commit. */
export const STREAM_REPARSE_MIN_CHARS = 1_200
/**
 * The gap is at least the last re-parse's measured duration × this factor: a re-parse
 * plus at least its own cost of quiet time, so the document's re-parse duty cycle stays
 * at or below 1/2 no matter how large the document grows.
 */
export const REPARSE_WORK_FACTOR = 2

type TableComponentProps = ComponentProps<'table'>

function tableComponent({ children, ...props }: TableComponentProps) {
  return (
    <div className="my-3 overflow-x-auto rounded-md border border-border">
      <table {...props}>{children}</table>
    </div>
  )
}

/** Stable across renders: the only component override, for wide tables. */
const TABLE_COMPONENTS = { table: tableComponent }

/**
 * The Markdown document, memoized at the subtree boundary.
 *
 * `Markdown` has no internal cache: it re-runs the whole remark/rehype pipeline and the
 * full element-tree reconciliation on EVERY render, so any parent re-render whose
 * `content` and `plugins` kept their references (the reparse-schedule re-check frame, the
 * cascade's own bookkeeping) would otherwise pay for a re-parse of the entire document.
 * Bailing here is what makes a re-check frame O(1) — the schedule decides when a re-parse
 * is owed, and this keeps an un-owed frame from doing the work.
 */
const RenderedDocument = memo(function RenderedDocument({
  content,
  plugins,
}: {
  content: string
  plugins: NonNullable<MarkdownProps['rehypePlugins']>
}) {
  chatWork.markdownRenders += 1
  return (
    <Markdown remarkPlugins={REMARK_PLUGINS} rehypePlugins={plugins} components={TABLE_COMPONENTS}>
      {content}
    </Markdown>
  )
})

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
 * The streaming answer's renderer, with a bounded reparse schedule (PLA-511).
 *
 * `content` is what the transport delivered; `displayed` is what the document on screen
 * actually shows. While a long answer is still growing, a longer `content` does not
 * re-run the whole Markdown/normalization/reveal pipeline on every commit — the last
 * document stays up, and a re-parse runs when the gap elapses (one re-check per frame
 * while visible, no timer while the document is hidden, and the return from hidden
 * reconciles). First text, resets, settled rows, and the terminal frame always re-parse
 * immediately, so the first word is not delayed and the final text never waits.
 *
 * Memoized on `content` so a stream that appends one token does not re-parse the
 * markdown for every other message on screen.
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
  // The document on screen: the last content this component actually parsed, paired with
  // the generation that parse was made under. The pair moves together, so the reveal
  // cascade never sees a new generation on an old document (a generation change with
  // its content must land in one commit, or the cascade would clear its schedule on the
  // document that is about to be replaced — PLA-501's reset contract).
  const [displayed, setDisplayed] = useState(content)
  const [displayedGeneration, setDisplayedGeneration] = useState(generation)
  /** Wall time the last re-parse was requested; the gap is measured from it. */
  const lastParseAtRef = useRef(0)
  /** What the last re-parse actually cost (the floor the gap scales to, below). */
  const lastParseDurationRef = useRef(0)
  /** A parse was requested on the previous commit; its cost is measured once committed. */
  const parseRequestedRef = useRef(false)
  /** A parse ran while the document was hidden: the return from hidden must not wait. */
  const lastParseHiddenRef = useRef(false)
  /**
   * The window just returned from hidden (PLA-511): any re-parse held while away is owed now,
   * visible or not. Set at the visibility boundary, consumed by the next decision.
   */
  const returnFromHiddenRef = useRef(false)
  /** The re-check frame for a held re-parse, while one is owed. */
  const recheckRafRef = useRef<number | null>(null)
  /** A pending re-check frame bumps this so the decision effect re-runs with fresh values. */
  const [recheckTick, setRecheckTick] = useState(0)

  // Returning from hidden is not a content change, so no dependency re-runs the decision on
  // its own: watch the boundary and re-check there, so held text reconciles the moment the
  // reader is back — no further token required.
  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.visibilityState !== 'visible') return
      returnFromHiddenRef.current = true
      setRecheckTick((tick) => tick + 1)
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => document.removeEventListener('visibilitychange', onVisibilityChange)
  }, [])

  useLayoutEffect(() => {
    if (recheckRafRef.current !== null) {
      cancelAnimationFrame(recheckRafRef.current)
      recheckRafRef.current = null
    }
    const generationChanged = generation !== displayedGeneration
    if (content === displayed && !generationChanged) {
      // A re-parse was requested on the previous commit and has now committed: measure
      // what it actually cost, so the next gap scales to the document's true re-parse
      // price (a document that costs more to re-parse than the floor cannot be
      // re-parsed on the floor — its cadence would degenerate to the baseline).
      if (parseRequestedRef.current) {
        lastParseDurationRef.current = Math.max(0, performance.now() - lastParseAtRef.current)
        parseRequestedRef.current = false
      }
      return
    }

    const now = performance.now()
    const hidden = document.visibilityState === 'hidden'
    // Immediate, no cadence: the first useful text (the reader has been waiting for
    // exactly this), a reset/clear, a new generation (a retry's words must not inherit
    // the replaced answer's slots — and the pair must move in one commit, see above), a
    // settled row (which always renders its whole document), the terminal frame (the last
    // words publish on this microtask, visible or hidden), and a return from hidden (the
    // held text must not wait out the gap).
    const force =
      generationChanged ||
      displayed === '' ||
      content === '' ||
      !streaming ||
      turnEnded ||
      returnFromHiddenRef.current ||
      (lastParseHiddenRef.current && !hidden)
    // The gap: the fixed floor, scaled up by what the last re-parse actually cost
    // (REPARSE_WORK_FACTOR), so a document whose re-parse exceeds the floor keeps its
    // re-parse duty cycle bounded instead of re-parsing on every commit.
    const gap = Math.max(STREAM_REPARSE_GAP_MS, lastParseDurationRef.current * REPARSE_WORK_FACTOR)
    // Short answers parse on every commit — the pipeline is sub-frame at that size, and
    // this is exactly the behavior the playback tests pin down. Long answers run on the
    // gap: at most one whole-document re-parse per gap.
    if (force || content.length < STREAM_REPARSE_MIN_CHARS || now - lastParseAtRef.current >= gap) {
      lastParseAtRef.current = now
      lastParseHiddenRef.current = hidden
      returnFromHiddenRef.current = false
      parseRequestedRef.current = true
      setDisplayed(content)
      setDisplayedGeneration(generation)
      return
    }
    // The re-parse is owed but not yet due. A hidden document is owed no frames: hold
    // the last document, and reconcile when the reader returns (or a terminal frame
    // arrives — those force). Visible: one re-check per frame, each an O(1) decision.
    if (hidden) return
    recheckRafRef.current = requestAnimationFrame(() => {
      recheckRafRef.current = null
      setRecheckTick((tick) => tick + 1)
    })
  }, [content, displayed, displayedGeneration, streaming, turnEnded, generation, recheckTick])

  // An unmounted renderer owes no frames: a held re-parse dies with it.
  useEffect(
    () => () => {
      if (recheckRafRef.current !== null) cancelAnimationFrame(recheckRafRef.current)
    },
    [],
  )

  // Everything below renders `displayed`, not `content`: the pipeline runs per actual
  // re-parse, not per content commit. The memo's dep on `streaming` is what applies a
  // pipeline change (a settle swap, a turn end) even when the text did not change.
  const renderContent = useMemo(() => {
    chatWork.markdownNormalizations += 1
    chatWork.normalizedChars += displayed.length
    return normalizeMarkdownForRender(displayed, streaming)
  }, [displayed, streaming])

  const rootRef = useRevealCascade({
    content: displayed,
    enabled: streaming,
    settled: turnEnded,
    onDrained: onRevealComplete,
    generation: displayedGeneration,
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

  // The reveal plugin reads the raw source (as plugin options) to anchor word identity,
  // and runs last: the equations it marks are already typeset. A new array reference is
  // what applies a pipeline change (a settle swap, a turn end) even when the text did not
  // change — and keeps the memoized document re-parsing exactly then.
  const rehypePlugins = useMemo(
    () =>
      (streaming
        ? [...REHYPE_PLUGINS, [rehypeRevealUnits, { rawSource: displayed }]]
        : REHYPE_PLUGINS) as NonNullable<MarkdownProps['rehypePlugins']>,
    [displayed, streaming],
  )

  return (
    <div ref={rootRef} className="assistant-content font-ai-response">
      <RenderedDocument content={renderContent} plugins={rehypePlugins} />
    </div>
  )
})
