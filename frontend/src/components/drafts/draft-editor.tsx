'use client'

import { CrepeBuilder } from '@milkdown/crepe/builder'
import { blockEdit } from '@milkdown/crepe/feature/block-edit'
import { codeMirror } from '@milkdown/crepe/feature/code-mirror'
import { cursor } from '@milkdown/crepe/feature/cursor'
import { latex } from '@milkdown/crepe/feature/latex'
import { linkTooltip } from '@milkdown/crepe/feature/link-tooltip'
import { listItem } from '@milkdown/crepe/feature/list-item'
import { placeholder } from '@milkdown/crepe/feature/placeholder'
import { table } from '@milkdown/crepe/feature/table'
import { toolbar } from '@milkdown/crepe/feature/toolbar'
import { editorViewCtx, EditorStatus } from '@milkdown/kit/core'
import type { Slice } from '@milkdown/kit/prose/model'
import type { EditorView } from '@milkdown/kit/prose/view'
import { getMarkdown, markdownToSlice, replaceAll } from '@milkdown/kit/utils'
import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react'

import {
  commentHighlightsPlugin,
  jumpToComment,
  setComments,
} from '@/components/drafts/comment-highlights'
import type { AnchorThread } from '@/components/drafts/comment-highlights'
import { citationHighlightsPlugin } from '@/components/drafts/citation-highlights'
import { writeSuggestionPlugin } from '@/components/drafts/write-suggestion'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'

// The application entrypoint imports KaTeX once, into its own cascade layer. A
// second unlayered import here would outrank every layer - including the chat's own
// `@layer components` KaTeX overrides - from the moment this chunk loaded.
import '@milkdown/crepe/theme/common/style.css'

/** What the workspace can ask of the editor once it exists. */
export interface DraftEditorHandle {
  /** The serialized document, or null before the editor has been created. */
  markdown(): string | null
  /** Replace the whole document, e.g. after a suggestion is accepted. */
  reset(markdown: string): void
  /** The live ProseMirror view, which is what the `/write` widget anchors to. */
  view(): EditorView | null
  /** Parse markdown into a slice with the live parser; null before the editor exists. */
  toSlice(markdown: string): Slice | null
  /** Re-anchor the margin comments' underlines against the document as it stands. */
  setComments(threads: AnchorThread[]): void
  /** Scroll to and flash one comment's anchor. False when it has none to jump to. */
  jumpToComment(commentId: number): boolean
}

type DraftEditorProps = {
  initialMarkdown: string
  onChange: (markdown: string) => void
  onEditorReady?: (view: EditorView) => void
  /** Opens the rail thread when its underline or gutter marker is activated. */
  onCommentClick?: (commentId: number) => void
  /** Opens the ledger entry behind an inline citation chip. */
  onSourceClick?: (sourceId: number) => void
  /**
   * The failed state's way out. The workspace hands it a real navigation - back to
   * the class that lists this draft - so the control is never a promise this file
   * cannot keep. Omitted, the exit control is not rendered at all.
   */
  onExit?: () => void
}

/**
 * One Crepe build, from constructor to teardown.
 *
 * `disposed` is the single guard every late callback checks: after unmount or after a
 * retry replaces the attempt, it stops the promise from attaching listeners, reporting
 * ready, or reporting changes for an editor nobody is looking at anymore. `live` marks
 * the attempt whose view the imperative handle serves and whose document may speak
 * through `onChange` - only a successfully created editor is ever live, so a create
 * that emits updates and then rejects schedules no autosave. `dom`/`click`/`keydown`
 * hold exactly what the component attached itself, so teardown can remove exactly
 * that. `host` is this attempt's own element inside the wrapper: two attempts never
 * share a DOM root, so a late initialization or teardown can never disturb a newer
 * retry or a StrictMode remount.
 */
type Attempt = {
  crepe: CrepeBuilder | null
  disposed: boolean
  live: boolean
  host: HTMLDivElement | null
  dom: HTMLElement | null
  click: ((event: MouseEvent) => void) | null
  keydown: ((event: KeyboardEvent) => void) | null
}

/**
 * Tear an editor down only once it is safe to destroy.
 *
 * The pinned Milkdown (`@milkdown/core` 7.22.0) lifecycle is: `create()` sets the
 * status to `OnCreate`, awaits every plugin, and on rejection never resets that
 * status; `destroy()` while the status is `OnCreate` reschedules itself every 50ms
 * and never completes. So destroying a create that rejected trades a finite leak
 * for an endless timer loop. Destroy is therefore gated on the editor's public
 * `status`: `Idle` (features threw before `create()` ran - destroy just releases the
 * configuration) and `Created` (the ordinary teardown, including a create that only
 * resolved after this component detached) are safe; `OnCreate` - a create that
 * rejected - is not destroyable through the public API at all. That leak is
 * Milkdown's, reported rather than papered over with a retry loop.
 */
function destroySettled(crepe: CrepeBuilder): void {
  if (crepe.editor.status === EditorStatus.OnCreate) return
  try {
    void Promise.resolve(crepe.destroy()).catch(() => {})
  } catch {
    // A destroy that throws synchronously is just its rejection by another name.
  }
}

/**
 * The draft document, as a Milkdown Crepe surface.
 *
 * Loaded lazily by the draft route so the editor is absent from the class-list startup
 * bundle. The feature set is deliberately small:
 * the toolbar, the block handle, lists, tables, links, and math, with no image block,
 * because a draft is prose and the class's documents already hold the figures. The code
 * editor is in only because math depends on it, not because a draft wants one. There is
 * no collab: Lyra drafts are single-writer, so the document never leaves the room it was
 * typed in.
 *
 * The component owns the editor's lifetime and nothing else. Autosave lives in the
 * workspace, which hears every change through `onChange`; the `/write` widget is handed
 * the live view through `onEditorReady`. When creation or setup fails - synchronously or
 * by rejection - it says so once, in place, with Retry (which reseeds from the same
 * supplied body) and an Exit that leads wherever the workspace says; the supplied body,
 * the save engine, and any confirmed writing on the server are untouched, and a failed
 * attempt never reports ready or files a change. Teardown is symmetric: the listeners
 * this component attached are the ones it removes, and a destroy that rejects is
 * silenced here rather than dropped on the floor as an unhandled promise.
 */
export const DraftEditor = forwardRef<DraftEditorHandle, DraftEditorProps>(function DraftEditor(
  { initialMarkdown, onChange, onEditorReady, onCommentClick, onSourceClick, onExit },
  ref,
) {
  const rootRef = useRef<HTMLDivElement>(null)
  const crepeRef = useRef<CrepeBuilder | null>(null)
  // Creation runs through an attempt lifecycle rather than as dependencies of the
  // effect, so the callbacks reach it through refs and a prop change never rebuilds
  // the editor behind the writer.
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  const onEditorReadyRef = useRef(onEditorReady)
  onEditorReadyRef.current = onEditorReady
  const onCommentClickRef = useRef(onCommentClick)
  onCommentClickRef.current = onCommentClick
  const onSourceClickRef = useRef(onSourceClick)
  onSourceClickRef.current = onSourceClick
  // Retry reseeds from what the workspace supplied at mount, so a failed attempt can
  // never eat the draft's text: there is nothing here that rewrites the seed.
  const initialMarkdownRef = useRef(initialMarkdown)

  const [phase, setPhase] = useState<'creating' | 'ready' | 'failed'>('creating')
  const aliveRef = useRef(false)
  const attemptRef = useRef<Attempt | null>(null)

  /** Retires one attempt: guards it, unattaches its listeners, isolates its host, destroys it if settled. */
  const teardown = useCallback((attempt: Attempt) => {
    attempt.disposed = true
    if (attempt.dom) {
      if (attempt.click) attempt.dom.removeEventListener('click', attempt.click)
      if (attempt.keydown) attempt.dom.removeEventListener('keydown', attempt.keydown)
    }
    attempt.dom = null
    attempt.click = null
    attempt.keydown = null
    if (attempt.live) {
      attempt.live = false
      if (crepeRef.current === attempt.crepe) crepeRef.current = null
    }
    // Detach before destroying: whatever the late create or destroy does to its own
    // tree, the document the writer is looking at is already out of reach.
    if (attempt.host) {
      attempt.host.remove()
      attempt.host = null
    }
    if (attempt.crepe) destroySettled(attempt.crepe)
  }, [])

  /**
   * Builds and creates one editor. At most one attempt is ever current: a retry
   * retires the previous one before starting, and a retired attempt's late
   * resolution or rejection destroys itself without touching the screen.
   */
  const startAttempt = useCallback(() => {
    const root = rootRef.current
    if (!root || !aliveRef.current) return
    const previous = attemptRef.current
    if (previous) {
      attemptRef.current = null
      teardown(previous)
    }

    setPhase('creating')
    // Each attempt writes into its own host element. The wrapper survives a retry
    // and a StrictMode remount; a stale attempt's create or destroy can only ever
    // touch its own detached host, never the editor that replaced it.
    const host = document.createElement('div')
    root.appendChild(host)
    const attempt: Attempt = {
      crepe: null,
      disposed: false,
      live: false,
      host,
      dom: null,
      click: null,
      keydown: null,
    }
    attemptRef.current = attempt

    try {
      const crepe = new CrepeBuilder({ root: host, defaultValue: initialMarkdownRef.current })
      // Recorded before a single feature runs: a feature that throws mid-chain is
      // still a constructed editor, and losing the reference to it would lose the
      // only chance at cleanup.
      attempt.crepe = crepe
      crepe
        .addFeature(toolbar)
        .addFeature(blockEdit)
        .addFeature(listItem)
        .addFeature(linkTooltip)
        .addFeature(table)
        // Ahead of `latex`, and not optional. A display equation in Crepe is a code block
        // with `latex` for its language, so the math feature is built on the code block
        // component and refuses to configure itself without one - by throwing, during
        // `create`, which is what met anyone who opened a draft. Its cost is a code block
        // that a draft has no particular use for; the alternative was no math at all.
        //
        // `previewOnlyByDefault` is what makes a display equation look like an equation.
        // Left unset it falls back to the editor's read-only flag - false here - so every
        // `$$...$$` opened as a CodeMirror box full of `\frac{...}` with a language picker
        // and a Copy button beside it, which is most of "the LaTeX formatting doesn't
        // work". It costs nothing for ordinary code blocks: the editor is only hidden
        // where a preview exists (`code-block.tsx`), and only `latex` renders one.
        .addFeature(codeMirror, { previewOnlyByDefault: true })
        .addFeature(latex)
        .addFeature(cursor)
        .addFeature(placeholder, { text: 'Start writing', mode: 'block' })
        // The `/write` suggestion block: a widget decoration, never document content until
        // accepted, so an un-accepted passage never reaches autosave or undo.
        .addFeature((editor) => {
          editor.use(writeSuggestionPlugin)
        })
        // Margin-comment anchors: severity-tinted underlines, likewise decorations and
        // never document content, so an anchor survives autosave untouched.
        .addFeature((editor) => {
          editor.use(commentHighlightsPlugin)
        })
        // Stable source ids remain markdown text on disk but read as clickable citation
        // chips in the document. The Sources tab owns their full ledger entries.
        .addFeature((editor) => {
          editor.use(citationHighlightsPlugin)
        })
      crepe.on((api) => {
        api.markdownUpdated((_ctx, markdown, prev) => {
          // Only a ready, still-current editor may speak for the draft. The listener
          // is registered before `create()` runs (Crepe defers `on` through editor
          // config), so an editor that emits updates during setup and then rejects
          // would otherwise schedule an autosave for a document that never opened -
          // and a disposed attempt is unmounted or superseded, its document belongs
          // to no writer any more.
          if (!attempt.live || attempt.disposed) return
          // The seed document's first report is not a change the writer made.
          if (prev != null && markdown !== prev) onChangeRef.current(markdown)
        })
      })
    } catch {
      // Construction or a feature's setup threw synchronously. The attempt is retired
      // like any other - its host detaches, and its editor, still `Idle` because
      // `create()` never ran, is destroyable and destroyed - and the supplied body is
      // kept exactly as it was.
      attempt.disposed = true
      if (attemptRef.current === attempt) attemptRef.current = null
      teardown(attempt)
      setPhase('failed')
      return
    }

    const crepe = attempt.crepe
    if (!crepe) return
    void crepe
      .create()
      .then(() => {
        if (attempt.disposed) {
          // Resolved after unmount or after a retry replaced it: the deferred
          // teardown. By now the create settled, so destroy is on safe footing.
          destroySettled(crepe)
          return
        }
        const view = crepe.editor.action((ctx) => ctx.get(editorViewCtx))
        const activateComment = (target: EventTarget | null) => {
          const element =
            target instanceof Element ? target.closest<HTMLElement>('[data-comment-id]') : null
          const id = Number(element?.dataset.commentId)
          if (Number.isSafeInteger(id) && id > 0) onCommentClickRef.current?.(id)
        }
        const activateSource = (target: EventTarget | null) => {
          const element =
            target instanceof Element ? target.closest<HTMLElement>('[data-source-id]') : null
          const id = Number(element?.dataset.sourceId)
          if (Number.isSafeInteger(id) && id > 0) onSourceClickRef.current?.(id)
        }
        // Recorded as attached before each call, so a failure partway through still
        // knows precisely which listeners teardown owes the document.
        attempt.dom = view.dom
        attempt.click = (event: MouseEvent) => {
          activateComment(event.target)
          activateSource(event.target)
        }
        view.dom.addEventListener('click', attempt.click)
        attempt.keydown = (event: KeyboardEvent) => {
          if (event.key !== 'Enter' && event.key !== ' ') return
          const target =
            event.target instanceof Element
              ? event.target.closest('[data-comment-id], [data-source-id]')
              : null
          if (!target) return
          event.preventDefault()
          activateComment(target)
          activateSource(target)
        }
        view.dom.addEventListener('keydown', attempt.keydown)
        attempt.live = true
        crepeRef.current = crepe
        setPhase('ready')
        try {
          onEditorReadyRef.current?.(view)
        } catch {
          // The editor is live and was handed over; the workspace's own ready handler
          // failing is its business, and un-writing the handover would be the lie.
        }
      })
      .catch(() => {
        // Creation, setup, or the ready wiring rejected. Nothing on this attempt is
        // live, and nothing about it may reach the workspace.
        if (attempt.disposed) {
          // Teardown already ran. A create that rejected leaves Milkdown stuck in
          // `OnCreate`, where destroy is the 50ms retry loop `destroySettled` refuses;
          // an already-created editor that failed mid-ready was never disposed here,
          // but then it also never reported ready, so the host is simply abandoned
          // with its attempt.
          return
        }
        if (attemptRef.current === attempt) attemptRef.current = null
        teardown(attempt)
        if (aliveRef.current) setPhase('failed')
      })
  }, [teardown])

  useEffect(() => {
    aliveRef.current = true
    startAttempt()
    return () => {
      aliveRef.current = false
      const attempt = attemptRef.current
      attemptRef.current = null
      if (attempt) teardown(attempt)
    }
  }, [startAttempt, teardown])

  useImperativeHandle(
    ref,
    () => ({
      markdown: () => crepeRef.current?.editor.action(getMarkdown()) ?? null,
      reset: (markdown: string) => {
        crepeRef.current?.editor.action(replaceAll(markdown))
      },
      view: () => crepeRef.current?.editor.action((ctx) => ctx.get(editorViewCtx)) ?? null,
      toSlice: (markdown: string) =>
        crepeRef.current?.editor.action(markdownToSlice(markdown)) ?? null,
      setComments: (threads: AnchorThread[]) => {
        crepeRef.current?.editor.action((ctx) => setComments(ctx.get(editorViewCtx), threads))
      },
      jumpToComment: (commentId: number) =>
        crepeRef.current?.editor.action((ctx) =>
          jumpToComment(ctx.get(editorViewCtx), commentId),
        ) ?? false,
    }),
    [],
  )

  return (
    <>
      {phase === 'failed' ? (
        <Alert variant="destructive" className="mb-4">
          <AlertTitle>Could not open the editor</AlertTitle>
          <AlertDescription>
            Your draft text is safe and unchanged. Try opening the editor again, or exit to the
            class and come back to this draft later.
          </AlertDescription>
          <div className="flex flex-wrap gap-2 pt-2">
            <Button size="sm" onClick={startAttempt}>
              Retry
            </Button>
            {onExit ? (
              <Button size="sm" variant="outline" onClick={onExit}>
                Exit to class
              </Button>
            ) : null}
          </div>
        </Alert>
      ) : null}
      <div
        ref={rootRef}
        className="draft-editor"
        hidden={phase === 'failed'}
        aria-busy={phase === 'creating' ? true : undefined}
      />
    </>
  )
})
