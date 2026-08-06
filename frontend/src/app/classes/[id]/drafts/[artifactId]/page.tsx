'use client'

import { useQueryClient } from '@tanstack/react-query'
import { Camera, Pencil, Printer, Sparkles, Wand2 } from 'lucide-react'
import dynamic from 'next/dynamic'
import { useParams } from 'next/navigation'
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'

import type { DraftEditorHandle } from '@/components/drafts/draft-editor'
import { SuggestionPanel } from '@/components/drafts/suggestion-panel'
import { startWrite } from '@/components/drafts/write-suggestion'
import { HeaderCrumb, useFullBleed } from '@/components/layout/page-chrome'
import { RevisionHistory } from '@/components/solutions/revision-history'
import { StepThread } from '@/components/solutions/step-thread'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '@/components/ui/spinner'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { ApiError } from '@/lib/api'
import { createSaveEngine, flushOnHidden } from '@/lib/drafts/save-engine'
import type { SaveStateName } from '@/lib/drafts/save-engine'
import { useClasses } from '@/lib/hooks/use-classes'
import {
  draftKeys,
  useDraft,
  useDraftStatus,
  usePendingEdit,
  useRenameDraft,
  useSuggest,
  useUpdateBody,
} from '@/lib/hooks/use-drafts'
import { cn } from '@/lib/utils'
import type { AcceptRejectResult, DraftDetail, PendingEdit, SolutionPart } from '@/types'

// The editor is a DOM creature: no server render, and a skeleton while the chunk lands.
const DraftEditor = dynamic(
  () => import('@/components/drafts/draft-editor').then((loaded) => loaded.DraftEditor),
  {
    ssr: false,
    loading: () => <Skeleton className="h-96 w-full rounded-md" />,
  },
)

function readId(value: string | string[] | undefined): number | null {
  const raw = Array.isArray(value) ? value[0] : value
  const parsed = Number(raw)
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null
}

type RailTab = 'suggestion' | 'history' | 'chat'

export default function DraftWorkspacePage() {
  const params = useParams<{ id: string; artifactId: string }>()
  const classId = readId(params.id)
  const artifactId = readId(params.artifactId)
  const queryClient = useQueryClient()

  const draft = useDraft(artifactId ?? Number.NaN)
  const status = useDraftStatus(artifactId ?? Number.NaN, artifactId !== null)
  const pending = usePendingEdit(artifactId ?? Number.NaN, artifactId !== null)
  const classes = useClasses()
  const rename = useRenameDraft(classId ?? Number.NaN)
  const suggest = useSuggest(artifactId ?? Number.NaN)
  const updateBody = useUpdateBody(artifactId ?? Number.NaN)

  const editorRef = useRef<DraftEditorHandle | null>(null)
  /** The document as the editor last reported it, which is what a flush writes. */
  const latestMarkdownRef = useRef('')
  const [latestMarkdown, setLatestMarkdown] = useState('')
  const [saveState, setSaveState] = useState<SaveStateName>('saved')
  const [saveDetail, setSaveDetail] = useState<string | null>(null)
  const [suggestOpen, setSuggestOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [railTab, setRailTab] = useState<RailTab>('chat')
  const chatScrollRef = useRef<HTMLDivElement | null>(null)

  // The save engine is created once: the editor's onChange and the visibility flush both
  // talk to it, and rebuilding it per render would drop a scheduled write on the floor.
  // `mutateAsync` is stable across renders, so the closure never goes stale.
  const [engine] = useState(() =>
    createSaveEngine({
      write: (content) => updateBody.mutateAsync({ content }).then(() => undefined),
      onState: (state, detail) => {
        setSaveState(state)
        setSaveDetail(detail ?? null)
      },
    }),
  )

  // The poll is the live source of truth for a suggestion run; when it moves, the detail
  // and the pending edit are stale, exactly as the study page treats its own poll.
  const polledState = status.data?.state
  useEffect(() => {
    if (!polledState || artifactId === null) return
    queryClient.invalidateQueries({ queryKey: draftKeys.detail(artifactId) })
    queryClient.invalidateQueries({ queryKey: draftKeys.pending(artifactId) })
  }, [polledState, artifactId, queryClient])

  // Flush on the way out: a hidden tab is the last moment a write can still be sent, and
  // an unmount drops the editor entirely.
  useEffect(() => {
    const detach = flushOnHidden(() => {
      void engine.flush(latestMarkdownRef.current)
    })
    return () => {
      detach()
      void engine.flush(latestMarkdownRef.current)
    }
  }, [engine])

  /** Open the `/write` block at the caret: the toolbar button and Mod-/ share this. */
  const openWrite = useCallback(() => {
    const handle = editorRef.current
    const view = handle?.view()
    if (!handle || !view || artifactId === null) return
    startWrite(view, {
      draftId: artifactId,
      toSlice: (markdown) => {
        const slice = handle.toSlice(markdown)
        if (!slice) throw new Error('The editor is not ready yet.')
        return slice
      },
    })
  }, [artifactId])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key === '/') {
        event.preventDefault()
        openWrite()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [openWrite])

  // The pending edit as the panel last resolved it, falling back to the query. The
  // override keeps the panel on what the server just answered instead of waiting a round
  // trip; the next query result (same content) retires it. Adjusted during render, the
  // same pattern the study panel uses for its dialog state.
  const [editOverride, setEditOverride] = useState<PendingEdit | null>(null)
  const pendingData = pending.data
  const [pendingDataSeen, setPendingDataSeen] = useState(pendingData)
  if (pendingData !== pendingDataSeen) {
    setPendingDataSeen(pendingData)
    setEditOverride(null)
  }
  const edit = editOverride ?? pendingData ?? null

  // A fresh edit opens the rail on it; a resolved one hands the rail back to the chat.
  const [editSeenId, setEditSeenId] = useState<number | null>(null)
  if (edit && editSeenId !== edit.id) {
    setEditSeenId(edit.id)
    setRailTab('suggestion')
  }
  if (!edit && editSeenId !== null) {
    setEditSeenId(null)
    if (railTab === 'suggestion') setRailTab('chat')
  }

  const loaded = draft.data ?? null
  useFullBleed(loaded !== null)

  // Derived before the guards, because hooks cannot sit after an early return. The body
  // part object is the minimal SolutionPart shape RevisionHistory and StepThread read:
  // revisions, restore, and step-scoped chat work unchanged on a draft's one part.
  const bodyPart = useMemo<SolutionPart | null>(() => {
    if (!loaded) return null
    return {
      id: loaded.part_id,
      artifact_id: loaded.id,
      parent_part_id: null,
      kind: 'draft_body',
      ordinal: 1,
      label: loaded.title,
      content: loaded.body,
      content_type: 'markdown',
      status: 'complete',
      origin: 'generated',
      verdict: 'unchecked',
      verdict_detail: null,
      solve_parts: 'together',
      error_message: null,
      provenance: [],
      checks: [],
    }
  }, [loaded])

  /** After any accept: pull the refetched body back into the editor and the engine. */
  async function syncEditorFromServer() {
    if (artifactId === null) return
    await queryClient.invalidateQueries({ queryKey: draftKeys.detail(artifactId) })
    const fresh = queryClient.getQueryData<DraftDetail>(draftKeys.detail(artifactId))
    if (!fresh || fresh.body === latestMarkdownRef.current) return
    engine.noteSaved(fresh.body)
    latestMarkdownRef.current = fresh.body
    setLatestMarkdown(fresh.body)
    editorRef.current?.reset(fresh.body)
    setSaveState('saved')
  }

  function onSuggestionApplied(result: AcceptRejectResult) {
    if (result.remaining === 0) {
      setEditOverride(null)
      setRailTab('chat')
    } else if (result.edit) {
      setEditOverride(result.edit)
    }
    void syncEditorFromServer()
  }

  async function onSnapshot() {
    const content = latestMarkdownRef.current
    try {
      await updateBody.mutateAsync({ content, snapshot: true })
      // The snapshot wrote the content too, so nothing is dirty any longer.
      engine.cancel()
      engine.noteSaved(content)
      setSaveState('saved')
      toast.success('Snapshot saved to the history.')
    } catch (caught) {
      toast.error(caught instanceof ApiError ? caught.message : 'Could not save a snapshot.')
    }
  }

  if (classId === null || artifactId === null) {
    return (
      <Alert variant="destructive">
        <AlertTitle>That link is not valid</AlertTitle>
        <AlertDescription>Open a draft from your class workspace.</AlertDescription>
      </Alert>
    )
  }

  if (draft.isPending) {
    return (
      <div className="mx-auto flex w-full max-w-[860px] flex-col gap-4" aria-busy="true">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-4 w-40" />
        <Skeleton className="h-96 w-full rounded-md" />
      </div>
    )
  }

  if (draft.isError) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load this draft</AlertTitle>
        <AlertDescription>
          <p>{draft.error instanceof ApiError ? draft.error.message : 'Something went wrong.'}</p>
          <Button variant="outline" size="sm" className="mt-3" onClick={() => void draft.refetch()}>
            Retry
          </Button>
        </AlertDescription>
      </Alert>
    )
  }

  const artifact = draft.data
  const state = polledState ?? artifact.state
  const stageDetail = status.data?.stage_detail ?? artifact.stage_detail
  const errorMessage = status.data?.error_message ?? artifact.error_message
  const generating = state === 'pending' || state === 'generating'
  const className = classes.data?.find((entry) => entry.id === classId)?.name ?? 'Class'

  return (
    <div className="flex min-h-0 w-full flex-1 flex-col">
      <HeaderCrumb>{artifact.title}</HeaderCrumb>

      {/* The writing desk's own header: the title (editable in place), the honest save
          state, and the four things you do to a draft. None of it prints. */}
      <header className="border-border flex flex-wrap items-center gap-2 border-b px-4 py-2 md:px-6 print:hidden">
        <DraftTitle
          title={artifact.title}
          pending={rename.isPending}
          onRename={(title) =>
            rename.mutate(
              { draftId: artifact.id, title },
              {
                onError: (caught) =>
                  toast.error(
                    caught instanceof ApiError ? caught.message : 'Could not rename this draft.',
                  ),
              },
            )
          }
        />
        <SaveStateIndicator state={saveState} detail={saveDetail} />
        <div className="ml-auto flex items-center gap-1.5">
          <Button variant="ghost" size="sm" onClick={openWrite} title="Draft with AI (Ctrl-/)">
            <Sparkles className="size-4" />
            Draft with AI
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setSuggestOpen(true)}>
            <Wand2 className="size-4" />
            Suggest changes
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => void onSnapshot()}
            disabled={updateBody.isPending}
            title="Save a history point"
          >
            <Camera className="size-4" />
            Snapshot
          </Button>
          <Button variant="ghost" size="sm" onClick={() => window.print()} title="Print this draft">
            <Printer className="size-4" />
            Print
          </Button>
        </div>
      </header>

      {generating ? (
        <div
          className="border-border bg-accent-surface/40 flex items-center gap-2 border-b px-4 py-2 text-sm md:px-6 print:hidden"
          aria-live="polite"
        >
          <Spinner className="size-3.5" />
          <span className="text-text-secondary">
            {stageDetail ?? 'Lyra is drafting a suggestion.'}
          </span>
        </div>
      ) : null}

      {state === 'failed' ? (
        <Alert variant="destructive" className="m-4 w-auto print:hidden">
          <AlertTitle>Lyra could not finish that suggestion</AlertTitle>
          <AlertDescription>
            <p>{errorMessage ?? 'Something went wrong while working on it.'}</p>
            <Button
              variant="outline"
              size="sm"
              className="mt-3"
              onClick={() => setSuggestOpen(true)}
            >
              Try again
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}

      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        {/* The document column: centred to a reading measure, scrolling on its own. */}
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-[760px] px-6 py-8 md:px-10">
            <DraftEditor
              key={artifact.id}
              ref={editorRef}
              initialMarkdown={artifact.body}
              onChange={(markdown) => {
                latestMarkdownRef.current = markdown
                setLatestMarkdown(markdown)
                engine.schedule(markdown)
              }}
              onEditorReady={(view) => {
                // The engine starts from what the server holds, so the seed document is
                // not a change waiting to be written back.
                engine.noteSaved(artifact.body)
                latestMarkdownRef.current = artifact.body
                setLatestMarkdown(artifact.body)
                view.dom.setAttribute('aria-label', 'Draft document')
              }}
            />
          </div>
        </div>

        <aside
          className="border-border flex min-h-0 flex-col border-t lg:w-[380px] lg:border-t-0 lg:border-l print:hidden"
          aria-label="Draft tools"
        >
          <Tabs
            value={railTab}
            onValueChange={(value) => setRailTab(value as RailTab)}
            className="flex min-h-0 flex-1 flex-col gap-0"
          >
            <TabsList variant="line" aria-label="Draft tools" className="shrink-0 px-2">
              {edit ? <TabsTrigger value="suggestion">Suggestion</TabsTrigger> : null}
              <TabsTrigger value="history">History</TabsTrigger>
              <TabsTrigger value="chat">Chat</TabsTrigger>
            </TabsList>

            {edit ? (
              <TabsContent value="suggestion" className="min-h-0 flex-1 overflow-y-auto p-4">
                <SuggestionPanel
                  draftId={artifact.id}
                  edit={edit}
                  currentBody={latestMarkdown}
                  onApplied={onSuggestionApplied}
                />
              </TabsContent>
            ) : null}

            <TabsContent value="history" className="min-h-0 flex-1 overflow-y-auto p-4">
              <div className="flex flex-col gap-3">
                <p className="text-text-secondary text-sm">
                  Snapshots and accepted suggestions, newest first. Restoring one writes a new
                  version, so nothing is ever lost.
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  className="self-start"
                  onClick={() => setHistoryOpen(true)}
                >
                  View history
                </Button>
              </div>
            </TabsContent>

            {/* forceMount: a conversation survives a look at the history tab. */}
            <TabsContent
              value="chat"
              forceMount
              className="min-h-0 flex-1 data-[state=inactive]:hidden"
            >
              <div ref={chatScrollRef} className="h-full overflow-y-auto p-4">
                {bodyPart ? (
                  <StepThread
                    classId={classId}
                    className={className}
                    step={bodyPart}
                    scrollViewportRef={chatScrollRef}
                    onClose={() => setRailTab(edit ? 'suggestion' : 'history')}
                  />
                ) : null}
              </div>
            </TabsContent>
          </Tabs>
        </aside>
      </div>

      {bodyPart ? (
        <RevisionHistory
          artifactId={artifact.id}
          part={historyOpen ? bodyPart : null}
          noun="draft"
          onClose={() => {
            setHistoryOpen(false)
            // A restore writes the body server-side, so the editor follows it.
            void syncEditorFromServer()
          }}
        />
      ) : null}

      <SuggestDialog
        open={suggestOpen}
        pending={suggest.isPending}
        onOpenChange={setSuggestOpen}
        onSuggest={async (instruction) => {
          await suggest.mutateAsync(instruction)
          toast.success('Lyra is drafting a suggestion.')
        }}
      />
    </div>
  )
}

/**
 * The draft's name, editable in place: a pencil turns the heading into an input, Enter
 * or leaving the field commits, Escape backs out. An empty name is not a rename.
 */
function DraftTitle({
  title,
  pending,
  onRename,
}: {
  title: string
  pending: boolean
  onRename: (title: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(title)

  // Reset during render rather than in an effect, so a rename landing from the server is
  // reflected the next time the field opens rather than after a stale frame.
  const [titleSeen, setTitleSeen] = useState(title)
  if (title !== titleSeen) {
    setTitleSeen(title)
    setValue(title)
  }

  function commit() {
    const trimmed = value.trim()
    setEditing(false)
    if (trimmed && trimmed !== title) onRename(trimmed)
    else setValue(title)
  }

  if (editing) {
    return (
      <Input
        value={value}
        autoFocus
        disabled={pending}
        aria-label="Draft name"
        className="h-8 w-64 max-w-full text-base font-medium"
        onChange={(event) => setValue(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === 'Enter') commit()
          if (event.key === 'Escape') {
            setValue(title)
            setEditing(false)
          }
        }}
      />
    )
  }

  return (
    <div className="flex min-w-0 items-center gap-1">
      <h1 className="text-text-primary truncate text-base font-medium">{title}</h1>
      <Button
        variant="ghost"
        size="icon"
        className="text-text-tertiary hover:text-text-primary size-7 shrink-0"
        aria-label="Rename this draft"
        onClick={() => setEditing(true)}
      >
        <Pencil className="size-3.5" />
      </Button>
    </div>
  )
}

/** dirty, saving, saved, error: whether the words on screen are the words on disk. */
function SaveStateIndicator({ state, detail }: { state: SaveStateName; detail: string | null }) {
  const label =
    state === 'saved'
      ? 'Saved'
      : state === 'saving'
        ? 'Saving'
        : state === 'dirty'
          ? 'Unsaved changes'
          : 'Could not save'
  return (
    <span
      role="status"
      aria-live="polite"
      title={state === 'error' ? (detail ?? undefined) : undefined}
      className={cn(
        'flex items-center gap-1.5 text-xs',
        state === 'error' ? 'text-danger-text' : 'text-text-tertiary',
      )}
    >
      <span
        aria-hidden
        className={cn(
          'size-1.5 rounded-full',
          state === 'saved' && 'bg-success-text',
          state === 'saving' && 'bg-accent-primary animate-pulse',
          state === 'dirty' && 'bg-text-tertiary',
          state === 'error' && 'bg-danger-text',
        )}
      />
      {label}
    </span>
  )
}

/** The instruction a whole-document suggestion pass starts from. */
function SuggestDialog({
  open,
  pending,
  onOpenChange,
  onSuggest,
}: {
  open: boolean
  pending: boolean
  onOpenChange: (open: boolean) => void
  onSuggest: (instruction: string) => Promise<void>
}) {
  const instructionId = useId()
  const [instruction, setInstruction] = useState('')
  const [error, setError] = useState<string | null>(null)

  const [openSeen, setOpenSeen] = useState(open)
  if (open !== openSeen) {
    setOpenSeen(open)
    setInstruction('')
    setError(null)
  }

  async function submit() {
    const trimmed = instruction.trim()
    if (!trimmed) return
    try {
      await onSuggest(trimmed)
      onOpenChange(false)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not start the suggestion.')
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Suggest changes</DialogTitle>
          <DialogDescription>
            Lyra reads the whole draft and proposes a revision, which you then review piece by
            piece.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-2">
          <Label htmlFor={instructionId}>What should change?</Label>
          <Input
            id={instructionId}
            value={instruction}
            autoFocus
            autoComplete="off"
            placeholder="Tighten the introduction and cite the syllabus"
            onChange={(event) => setInstruction(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                void submit()
              }
            }}
          />
        </div>
        {error ? (
          <p className="text-danger-text text-sm" role="alert">
            {error}
          </p>
        ) : null}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            disabled={instruction.trim().length === 0 || pending}
            onClick={() => void submit()}
          >
            {pending ? <Spinner /> : null}
            Suggest changes
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
