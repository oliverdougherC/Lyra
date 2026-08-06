'use client'

import { CrepeBuilder } from '@milkdown/crepe/builder'
import { blockEdit } from '@milkdown/crepe/feature/block-edit'
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

import { writeSuggestionPlugin } from '@/components/drafts/write-suggestion'

import '@milkdown/crepe/theme/common/style.css'
import 'katex/dist/katex.min.css'

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
}

type DraftEditorProps = {
  initialMarkdown: string
  onChange: (markdown: string) => void
  onEditorReady?: (view: EditorView) => void
}

/**
 * The draft document, as a Milkdown Crepe surface.
 *
 * Loaded by the workspace through `next/dynamic` with `ssr: false` - the editor is a DOM
 * creature and cannot be rendered on the server. The feature set is deliberately small:
 * the toolbar, the block handle, lists, tables, links, and math, with no code editor and
 * no image block, because a draft is prose and the class's documents already hold the
 * figures. There is no collab: Lyra drafts are single-writer, so the document never
 * leaves the room it was typed in.
 *
 * The component owns the editor's lifetime and nothing else. Autosave lives in the
 * workspace, which hears every change through `onChange`; the `/write` widget is handed
 * the live view through `onEditorReady`.
 */
export const DraftEditor = forwardRef<DraftEditorHandle, DraftEditorProps>(function DraftEditor(
  { initialMarkdown, onChange, onEditorReady },
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
      .addFeature(latex)
      .addFeature(cursor)
      .addFeature(placeholder, { text: 'Start writing', mode: 'block' })
      // The `/write` suggestion block: a widget decoration, never document content until
      // accepted, so an un-accepted passage never reaches autosave or undo.
      .addFeature((editor) => {
        editor.use(writeSuggestionPlugin)
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
    }),
    [],
  )

  return <div ref={rootRef} className="draft-editor" />
})
