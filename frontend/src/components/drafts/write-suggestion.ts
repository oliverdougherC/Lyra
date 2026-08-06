/**
 * The `/write` streamed suggestion: Lyra drafts a passage into an in-document suggestion
 * block that the writer accepts or rejects. Adapted from kuhn's write-suggestion.ts, with
 * the agent runtime replaced by `POST /api/drafts/{id}/write` and the chrome replaced by
 * Lyra's tokens, lucide icons, and sonner.
 *
 * The architecture is kuhn's, kept whole:
 *
 *  1. The suggestion lives OUTSIDE the document until accepted. It renders as a
 *     ProseMirror widget decoration, not as document content, so un-accepted text never
 *     reaches autosave or undo history. The anchor is a mapped decoration position, so
 *     edits elsewhere do not misplace it.
 *  2. Stream as plain text; parse on accept. During streaming the raw text is shown
 *     (cheap, robust to partial markdown). Accept parses the full string into a slice via
 *     the live parser and inserts it in one transaction; the debounced save then persists
 *     it through the normal path.
 *  3. One suggestion is active at a time; starting another dismisses the first.
 */

import { Plugin, PluginKey } from '@milkdown/kit/prose/state'
import type { EditorState, Transaction } from '@milkdown/kit/prose/state'
import type { Slice } from '@milkdown/kit/prose/model'
import { Decoration, DecorationSet } from '@milkdown/kit/prose/view'
import type { EditorView } from '@milkdown/kit/prose/view'
import { $prose } from '@milkdown/kit/utils'
import { toast } from 'sonner'

import { streamWrite } from '@/lib/api'
import type { WriteRequest } from '@/types'

const key = new PluginKey<DecorationSet>('lyra-write-suggestion')
const WIDGET_KEY = 'lyra-write-suggestion'

type Meta = { type: 'set'; deco: Decoration } | { type: 'clear' }

/**
 * Decoration-set plugin holding the (at most one) suggestion widget. The widget DOM is
 * owned by a Suggestion controller; the plugin only remaps its position across concurrent
 * edits. Spread into the editor's feature list.
 */
export const writeSuggestionPlugin = $prose(
  () =>
    new Plugin<DecorationSet>({
      key,
      state: {
        init: () => DecorationSet.empty,
        apply(tr: Transaction, set: DecorationSet): DecorationSet {
          const meta = tr.getMeta(key) as Meta | undefined
          if (meta?.type === 'set') return DecorationSet.create(tr.doc, [meta.deco])
          if (meta?.type === 'clear') return DecorationSet.empty
          return set.map(tr.mapping, tr.doc)
        },
      },
      props: {
        decorations: (state: EditorState) => key.getState(state),
      },
    }),
)

export interface WriteOptions {
  draftId: number
  /** Parse accepted markdown into a slice with the live parser (see the workspace). */
  toSlice: (markdown: string) => Slice
}

let active: Suggestion | null = null

/** Open a `/write` suggestion block at the caret and await the instruction. */
export function startWrite(view: EditorView, opts: WriteOptions): void {
  active?.dismiss()
  active = new Suggestion(view, opts)
  active.mount()
}

type Phase = 'prompt' | 'streaming' | 'done' | 'error'

/** What the editor knows about where the passage will land. */
export interface WriteContext {
  heading: string | null
  selection: string
  nearby: string
}

/**
 * The body of the write request: the instruction plus whichever grounding exists. Empty
 * context fields stay off the wire rather than travelling as empty strings.
 */
export function buildWriteRequest(instruction: string, ctx: WriteContext): WriteRequest {
  return {
    instruction,
    heading: ctx.heading || undefined,
    selection: ctx.selection || undefined,
    nearby: ctx.nearby || undefined,
  }
}

/**
 * What the editor can tell the writer about the caret: the nearest heading at or before
 * it, any selection, and the text around it (the current top-level block plus its
 * predecessor, capped). Plain and exported so the gathering is unit-testable without an
 * editor: a hand-built ProseMirror state answers everything this asks.
 */
export function gatherWriteContext(state: EditorState): WriteContext {
  const { from, to, $from } = state.selection
  const selection = from !== to ? state.doc.textBetween(from, to, '\n', '\n') : ''

  // Nearest heading at or before the caret's top-level block.
  const caretTop = $from.depth >= 1 ? $from.before(1) : 0
  let heading: string | null = null
  state.doc.forEach((child, offset) => {
    if (offset <= caretTop && child.type.name === 'heading') heading = child.textContent
  })

  const index = $from.index(0)
  const current = state.doc.maybeChild(index)?.textContent ?? ''
  const previous = index > 0 ? (state.doc.maybeChild(index - 1)?.textContent ?? '') : ''
  const nearby = [previous, current].filter(Boolean).join('\n\n').slice(0, 600)

  return { heading, selection, nearby }
}

const prefersReducedMotion = (): boolean =>
  window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false

/** Lucide's check and x, inlined: the widget is plain DOM, outside React's reach. */
function iconSvg(paths: string): string {
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" ` +
    `fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ` +
    `stroke-linejoin="round" aria-hidden="true">${paths}</svg>`
  )
}
const CHECK_ICON = iconSvg('<path d="M20 6 9 17l-5-5"/>')
const X_ICON = iconSvg('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>')

class Suggestion {
  private readonly dom: HTMLElement
  private readonly textEl: HTMLElement
  private readonly input: HTMLInputElement
  private readonly noteEl: HTMLElement
  private readonly errorMsgEl: HTMLElement

  private readonly view: EditorView
  private readonly opts: WriteOptions
  private readonly ctx: WriteContext
  private phase: Phase = 'prompt'
  private instruction = ''
  private text = ''
  private revealed = 0
  private revealTimer: number | undefined
  private abort: AbortController | null = null
  // Esc cancels while the suggestion is being composed or streamed; bound at the document
  // level so it fires even when the (hidden) input lacks focus.
  private readonly onDocKey = (e: KeyboardEvent): void => {
    if (e.key === 'Escape' && (this.phase === 'prompt' || this.phase === 'streaming')) {
      e.preventDefault()
      this.dismiss()
    }
  }

  constructor(view: EditorView, opts: WriteOptions) {
    this.view = view
    this.opts = opts
    this.ctx = gatherWriteContext(view.state)
    const section = this.ctx.heading ? `"${this.ctx.heading}"` : 'the current section'

    this.dom = document.createElement('div')
    this.dom.className = 'write-suggestion'
    this.dom.dataset.phase = 'prompt'
    this.dom.setAttribute('contenteditable', 'false')

    const eyebrow = document.createElement('div')
    eyebrow.className = 'ws-eyebrow'
    const eyebrowText = document.createElement('span')
    eyebrowText.className = 'ws-eyebrow-text'
    eyebrowText.textContent = 'Lyra is drafting'
    const dot = document.createElement('span')
    dot.className = 'ws-dot'
    const close = document.createElement('button')
    close.type = 'button'
    close.className = 'ws-close'
    close.title = 'Dismiss'
    close.setAttribute('aria-label', 'Dismiss suggestion')
    close.innerHTML = X_ICON
    close.addEventListener('mousedown', (e) => e.preventDefault())
    close.addEventListener('click', () => this.dismiss())
    eyebrow.append(dot, eyebrowText, close)

    // Prompt phase: the inline instruction input keeps the flow in-document.
    this.input = document.createElement('input')
    this.input.className = 'ws-input'
    this.input.type = 'text'
    this.input.placeholder = 'What should Lyra draft?'
    this.input.setAttribute('aria-label', 'What should Lyra draft?')
    this.input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault()
        this.submit()
      }
      // Escape is handled by the document-level listener (it must also fire while
      // streaming, when this input is hidden).
    })
    const prompt = document.createElement('div')
    prompt.className = 'ws-prompt'
    prompt.append(this.input)

    // Streaming / done phase: the suggested text plus a blinking caret (CSS).
    this.textEl = document.createElement('div')
    this.textEl.className = 'ws-text'
    const caret = document.createElement('span')
    caret.className = 'ws-caret'
    this.textEl.append(caret) // caret stays after the text node; CSS hides it off-stream

    // Action row (done).
    const actions = document.createElement('div')
    actions.className = 'ws-actions'
    const accept = document.createElement('button')
    accept.type = 'button'
    accept.className = 'ws-accept'
    accept.innerHTML = `${CHECK_ICON}<span>Accept</span>`
    accept.addEventListener('mousedown', (e) => e.preventDefault())
    accept.addEventListener('click', () => this.accept())
    const reject = document.createElement('button')
    reject.type = 'button'
    reject.className = 'ws-reject'
    reject.textContent = 'Reject'
    reject.addEventListener('mousedown', (e) => e.preventDefault())
    reject.addEventListener('click', () => this.reject())
    this.noteEl = document.createElement('span')
    this.noteEl.className = 'ws-note'
    this.noteEl.textContent = `Drafted from ${section}`
    actions.append(accept, reject, this.noteEl)

    // Error row.
    const errorRow = document.createElement('div')
    errorRow.className = 'ws-error'
    this.errorMsgEl = document.createElement('span')
    this.errorMsgEl.className = 'ws-error-msg'
    const retry = document.createElement('button')
    retry.type = 'button'
    retry.className = 'ws-retry'
    retry.textContent = 'Retry'
    retry.addEventListener('mousedown', (e) => e.preventDefault())
    retry.addEventListener('click', () => void this.runTask())
    const dismiss = document.createElement('button')
    dismiss.type = 'button'
    dismiss.className = 'ws-dismiss'
    dismiss.textContent = 'Dismiss'
    dismiss.addEventListener('mousedown', (e) => e.preventDefault())
    dismiss.addEventListener('click', () => this.dismiss())
    errorRow.append(this.errorMsgEl, retry, dismiss)

    this.dom.append(eyebrow, prompt, this.textEl, actions, errorRow)
  }

  // ---- mount / position -----------------------------------------------------

  mount(): void {
    const pos = this.anchorPos()
    const deco = Decoration.widget(pos, this.dom, {
      key: WIDGET_KEY,
      side: 1,
      ignoreSelection: true,
      // The block hosts its own inputs and buttons: keep every event out of ProseMirror.
      stopEvent: () => true,
    })
    this.view.dispatch(this.view.state.tr.setMeta(key, { type: 'set', deco }))
    document.addEventListener('keydown', this.onDocKey, true)
    // Focus after ProseMirror has rendered the widget into the DOM.
    requestAnimationFrame(() => this.input.focus())
  }

  /** Anchor at the boundary after the caret's top-level block (renders in-flow). */
  private anchorPos(): number {
    const { $from } = this.view.state.selection
    const size = this.view.state.doc.content.size
    if ($from.depth === 0) return Math.min($from.pos, size)
    return Math.min($from.after(1), size)
  }

  /** The live (mapped) widget position, or null once it has been cleared. */
  private currentPos(): number | null {
    const found = key.getState(this.view.state)?.find()
    return found && found.length ? found[0].from : null
  }

  // ---- stream lifecycle -----------------------------------------------------

  private submit(): void {
    const value = this.input.value.trim()
    if (!value) return
    this.instruction = value
    void this.runTask()
  }

  private async runTask(): Promise<void> {
    this.setPhase('streaming')
    this.text = ''
    this.revealed = 0
    this.renderText()
    this.abort = new AbortController()

    try {
      await streamWrite(
        this.opts.draftId,
        buildWriteRequest(this.instruction, this.ctx),
        (event) => {
          if (event.type === 'token') {
            this.text += event.text
            this.scheduleReveal()
          } else if (event.type === 'error') {
            this.showError(event.message || 'The write failed.')
          }
        },
        this.abort.signal,
      )
      this.finalizeStream()
    } catch (error) {
      if (this.abort?.signal.aborted) return // cancelled: the block is already gone
      this.showError((error as Error).message || 'The write failed.')
    }
  }

  // ---- reveal animation -----------------------------------------------------

  private scheduleReveal(): void {
    if (prefersReducedMotion()) {
      this.revealed = this.text.length
      this.renderText()
      return
    }
    if (this.revealTimer !== undefined) return
    const step = (): void => {
      this.revealTimer = undefined
      if (this.revealed >= this.text.length) return
      this.revealed = Math.min(this.text.length, this.revealed + 2)
      this.renderText()
      if (this.revealed < this.text.length) this.revealTimer = window.setTimeout(step, 18)
    }
    this.revealTimer = window.setTimeout(step, 18)
  }

  private renderText(): void {
    // textContent replaces every child; the caret span is re-appended so it survives.
    const caret = this.textEl.querySelector('.ws-caret')
    this.textEl.textContent = this.text.slice(0, this.revealed)
    if (caret) this.textEl.append(caret)
  }

  private finalizeStream(): void {
    if (this.phase !== 'streaming') return
    this.clearRevealTimer()
    this.revealed = this.text.length
    this.renderText()
    if (!this.text.trim()) {
      this.showError('Lyra returned an empty suggestion.')
      return
    }
    this.setPhase('done')
  }

  // ---- accept / reject / error ----------------------------------------------

  private accept(): void {
    const pos = this.currentPos()
    if (pos == null) {
      this.cleanup()
      return
    }
    let slice: Slice
    try {
      slice = this.opts.toSlice(this.text.trim())
    } catch {
      this.showError('Could not parse the suggestion into the document.')
      return
    }
    // One transaction: insert the parsed nodes and drop the widget. The workspace's
    // debounced autosave persists it through the normal path.
    const tr = this.view.state.tr.replace(pos, pos, slice)
    tr.setMeta(key, { type: 'clear' })
    this.view.dispatch(tr.scrollIntoView())
    this.flashInserted(pos)
    this.detach()
    toast.success('Suggestion accepted.')
    this.view.focus()
  }

  private reject(): void {
    this.cleanup()
    this.view.focus()
  }

  /** Cancel from outside (a new `/write`, Esc, the close button): abort and drop. */
  dismiss(): void {
    this.abort?.abort()
    this.cleanup()
    this.view.focus()
  }

  private showError(message: string): void {
    this.clearRevealTimer()
    this.errorMsgEl.textContent = message
    this.setPhase('error')
  }

  /** Highlight the freshly inserted block (fade-and-rise; reduced-motion safe). */
  private flashInserted(pos: number): void {
    if (prefersReducedMotion()) return
    try {
      const { node } = this.view.domAtPos(pos + 1)
      const el = (node.nodeType === 1 ? node : node.parentElement) as HTMLElement | null
      const block = el?.closest('.milkdown > * > *, .milkdown > *') ?? el
      if (block instanceof HTMLElement) {
        block.classList.add('ws-inserted')
        window.setTimeout(() => block.classList.remove('ws-inserted'), 360)
      }
    } catch {
      // The entrance animation is best-effort; the insertion has already happened.
    }
  }

  // ---- helpers ----------------------------------------------------------------

  private setPhase(phase: Phase): void {
    this.phase = phase
    this.dom.dataset.phase = phase
  }

  private clearRevealTimer(): void {
    clearTimeout(this.revealTimer)
    this.revealTimer = undefined
  }

  /** Drop the widget decoration (if still present) and release the singleton. */
  private cleanup(): void {
    if (this.currentPos() != null) {
      this.view.dispatch(this.view.state.tr.setMeta(key, { type: 'clear' }))
    }
    this.detach()
  }

  private detach(): void {
    this.clearRevealTimer()
    document.removeEventListener('keydown', this.onDocKey, true)
    if (active === this) active = null
  }
}
