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
import { editorViewCtx } from '@milkdown/kit/core'
import type { Slice } from '@milkdown/kit/prose/model'
import type { EditorView } from '@milkdown/kit/prose/view'
import { getMarkdown, markdownToSlice, replaceAll } from '@milkdown/kit/utils'
import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react'

import {
  commentHighlightsPlugin,
  jumpToComment,
  setComments,
} from '@/components/drafts/comment-highlights'
import type { AnchorThread } from '@/components/drafts/comment-highlights'
import { citationHighlightsPlugin } from '@/components/drafts/citation-highlights'
import { writeSuggestionPlugin } from '@/components/drafts/write-suggestion'

// KaTeX's stylesheet is imported once, into its own cascade layer, in globals.css. A
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
}

/**
 * The draft document, as a Milkdown Crepe surface.
 *
 * Loaded lazily by the draft route so the editor is absent from the class-list startup
 * bundle. The feature set is deliberately small:
 * the toolbar, the block handle, lists, tables, links, and math, with no image block,
 * because a draft is prose and the class's documents already hold the figures. The code
 * editor is in only because math depends on it, not because a draft wants one. There is no
 * collab: Lyra drafts are single-writer, so the document never leaves the room it was
 * typed in.
 *
 * The component owns the editor's lifetime and nothing else. Autosave lives in the
 * workspace, which hears every change through `onChange`; the `/write` widget is handed
 * the live view through `onEditorReady`.
 */
export const DraftEditor = forwardRef<DraftEditorHandle, DraftEditorProps>(function DraftEditor(
  { initialMarkdown, onChange, onEditorReady, onCommentClick, onSourceClick },
  ref,
) {
  const rootRef = useRef<HTMLDivElement>(null)
  const crepeRef = useRef<CrepeBuilder | null>(null)
  // The creation effect runs exactly once, so the callbacks reach it through refs rather
  // than as dependencies, which would otherwise rebuild the editor on every render.
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  const onEditorReadyRef = useRef(onEditorReady)
  onEditorReadyRef.current = onEditorReady
  const onCommentClickRef = useRef(onCommentClick)
  onCommentClickRef.current = onCommentClick
  const onSourceClickRef = useRef(onSourceClick)
  onSourceClickRef.current = onSourceClick
  const initialMarkdownRef = useRef(initialMarkdown)

  useEffect(() => {
    const root = rootRef.current
    if (!root) return
    let cancelled = false

    const crepe = new CrepeBuilder({ root, defaultValue: initialMarkdownRef.current })
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
        // The seed document's first report is not a change the writer made.
        if (prev != null && markdown !== prev) onChangeRef.current(markdown)
      })
    })

    void crepe.create().then(() => {
      if (cancelled) {
        void crepe.destroy()
        return
      }
      crepeRef.current = crepe
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
      view.dom.addEventListener('click', (event) => {
        activateComment(event.target)
        activateSource(event.target)
      })
      view.dom.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' && event.key !== ' ') return
        const target =
          event.target instanceof Element
            ? event.target.closest('[data-comment-id], [data-source-id]')
            : null
        if (!target) return
        event.preventDefault()
        activateComment(target)
        activateSource(target)
      })
      onEditorReadyRef.current?.(view)
    })

    return () => {
      cancelled = true
      crepeRef.current = null
      void crepe.destroy()
    }
  }, [])

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

  return <div ref={rootRef} className="draft-editor" />
})
