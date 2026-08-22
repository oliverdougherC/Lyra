'use client'

import { useQueryClient } from '@tanstack/react-query'
import {
  Camera,
  FileDown,
  Maximize2,
  Minimize2,
  PanelLeftClose,
  PanelRightClose,
  Pencil,
  Printer,
  SearchCheck,
} from 'lucide-react'
import dynamic from 'next/dynamic'
import { useParams } from 'next/navigation'
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'

import { ChatPane } from '@/components/chat/chat-pane'
import { BriefCard } from '@/components/drafts/brief-card'
import type { AnchorThread } from '@/components/drafts/comment-highlights'
import { CommentList } from '@/components/drafts/comment-list'
import { DraftEntryActions } from '@/components/drafts/draft-entry-actions'
import type { DraftEditorHandle } from '@/components/drafts/draft-editor'
import { LiveDraftSuggestionPanel } from '@/components/drafts/live-draft-suggestion'
import { PlanPanel } from '@/components/drafts/plan-panel'
import { SourceLedger } from '@/components/drafts/source-ledger'
import { SuggestionPanel } from '@/components/drafts/suggestion-panel'
import { startWrite } from '@/components/drafts/write-suggestion'
import { HeaderCrumb, useFullBleed, useImmersiveChrome } from '@/components/layout/page-chrome'
import { RevisionHistory } from '@/components/solutions/revision-history'
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '@/components/ui/spinner'
import { Switch } from '@/components/ui/switch'
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from '@/components/ui/resizable'
import { Tabs, TabsContent } from '@/components/ui/tabs'
import { api, ApiError, DraftBodyConflictError } from '@/lib/api'
import { SaveStateIndicator } from '@/components/drafts/save-state-indicator'
import { normalizeMathDelimiters } from '@/lib/drafts/math-delimiters'
import { createSaveEngine, decideServerSync, flushOnHidden } from '@/lib/drafts/save-engine'
import type { SaveConflict, SaveStateName } from '@/lib/drafts/save-engine'
import { useClasses } from '@/lib/hooks/use-classes'
import { useLocalStorageState } from '@/lib/hooks/use-local-storage-state'
import { useMediaQuery } from '@/lib/hooks/use-media-query'
import { chatKeys } from '@/lib/hooks/use-chat'
import {
  useCancelDraftRun,
  draftKeys,
  useComments,
  useDraft,
  useDraftStatus,
  useExportAvailability,
  useLiveDraftSuggestion,
  usePendingEdit,
  useRenameDraft,
  useStartPass,
  useStartReview,
  useUpdateBody,
  useWriterSessions,
} from '@/lib/hooks/use-drafts'
import { cn } from '@/lib/utils'
import type {
  AcceptRejectResult,
  DraftDetail,
  LiveDraftSuggestion,
  PassRequest,
  PendingEdit,
  SolutionPart,
  WriterDepth,
} from '@/types'

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

type RailTab = 'live' | 'suggestion' | 'plan' | 'sources' | 'comments' | 'history' | 'chat'

/**
 * Where the tools sit, and whether the application is on screen at all.
 *
 * Both are the writer's, not the draft's, so they are keyed per person rather than per
 * document: someone who writes left-handed writes left-handed in every draft, and being
 * asked again on the next one is the whole complaint. `localStorage` rather than the
 * settings row because neither is anyone's business but this browser's, and a preference
 * that needs a round trip to be honoured shows the wrong layout first.
 */
const RAIL_SIDE_KEY = 'lyra-draft-rail-side'
const RAIL_SHARE_KEY = 'lyra-draft-rail-share'
const IMMERSIVE_KEY = 'lyra-draft-immersive'

/** Percent of the split the tools take when the student has not moved it. */
const DEFAULT_RAIL_SHARE = 30
const MIN_RAIL_SHARE = 20
const MAX_RAIL_SHARE = 45

type RailSide = 'left' | 'right'

function parseSide(raw: string): RailSide | null {
  return raw === 'left' || raw === 'right' ? raw : null
}

function parseShare(raw: string): number | null {
  const value = Number(raw)
  if (!Number.isFinite(value)) return null
  // Clamped on the way in: a stored width from a window that no longer exists, or a key
  // edited by hand, must not be able to leave the document without room to read.
  return Math.min(MAX_RAIL_SHARE, Math.max(MIN_RAIL_SHARE, value))
}

/** One panel's percentage out of a layout the panel group reported. */
function shareOf(layout: Record<string, number>, id: string): number | null {
  const value = layout[id]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function parseImmersive(raw: string): boolean | null {
  return raw === 'true' ? true : raw === 'false' ? false : null
}

export default function DraftWorkspacePage() {
  const params = useParams<{ id: string; artifactId: string }>()
  const classId = readId(params.id)
  const artifactId = readId(params.artifactId)
  const queryClient = useQueryClient()

  const draft = useDraft(artifactId ?? Number.NaN)
  const status = useDraftStatus(artifactId ?? Number.NaN, artifactId !== null)

  // Two background residents share the poll, told apart by the stage detail's
  // server-side contract: a review's details all start with "Reviewing", and a review
  // never writes the document - so the student keeps the pen while one runs. A draft
  // pass is the opposite: it owns the document, and the editor follows it. Derived up
  // here because the comments query below polls while a review is in flight.
  const polledState = status.data?.state
  const polledDetail = status.data?.stage_detail
  const polledJobKind = status.data?.job_kind
  const jobRunning = polledState === 'pending' || polledState === 'generating'
  const reviewRunning =
    jobRunning &&
    (polledJobKind === 'review' ||
      (polledJobKind == null && (polledDetail?.startsWith('Reviewing') ?? false)))
  const passRunning = jobRunning && !reviewRunning

  const pending = usePendingEdit(artifactId ?? Number.NaN, artifactId !== null)
  const classes = useClasses()
  const rename = useRenameDraft(classId ?? Number.NaN)
  const startPass = useStartPass(artifactId ?? Number.NaN)
  const startReview = useStartReview(artifactId ?? Number.NaN)
  const cancelRun = useCancelDraftRun(artifactId ?? Number.NaN)
  const updateBody = useUpdateBody(artifactId ?? Number.NaN)
  const writerSessions = useWriterSessions(artifactId ?? Number.NaN, artifactId !== null)
  const commentThreads = useComments(artifactId ?? Number.NaN, artifactId !== null, reviewRunning)
  const exportability = useExportAvailability()
  const liveSuggestion = useLiveDraftSuggestion(
    artifactId ?? Number.NaN,
    artifactId !== null,
    passRunning,
  )
  const [exporting, setExporting] = useState(false)

  const editorRef = useRef<DraftEditorHandle | null>(null)
  /** The document as the editor last reported it, which is what a flush writes. */
  const latestMarkdownRef = useRef('')
  const [latestMarkdown, setLatestMarkdown] = useState('')
  const [saveState, setSaveState] = useState<SaveStateName>('saved')
  const [saveDetail, setSaveDetail] = useState<string | null>(null)
  const [draftDialogOpen, setDraftDialogOpen] = useState(false)
  const [reviewOpen, setReviewOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [railTab, setRailTab] = useState<RailTab>('chat')
  /**
   * The writer conversation on screen: the one this visit created, else the draft's
   * newest. Held locally so the first message's session lands here the moment it exists
   * rather than after a list refetch.
   */
  const [writerSessionId, setWriterSessionId] = useState<number | null>(null)
  const activeWriterSessionId = writerSessionId ?? writerSessions.data?.[0]?.id ?? null
  const [railSide, setRailSide] = useLocalStorageState<RailSide>(RAIL_SIDE_KEY, 'right', parseSide)
  // The split is the student's, and it is remembered. Below `xl` there is one column and
  // nothing to split, so the group is not rendered at all rather than being disabled.
  const wide = useMediaQuery('(min-width: 1280px)')
  const [railShare, setRailShare] = useLocalStorageState(
    RAIL_SHARE_KEY,
    DEFAULT_RAIL_SHARE,
    parseShare,
  )
  const documentFirstLayout = { document: 100 - railShare, rail: railShare }
  const railFirstLayout = { rail: railShare, document: 100 - railShare }
  const [immersive, setImmersive] = useLocalStorageState(IMMERSIVE_KEY, false, parseImmersive)
  const chatScrollRef = useRef<HTMLDivElement | null>(null)
  const railTabsRef = useRef<HTMLDivElement | null>(null)

  // The save engine is created once: the editor's onChange and the visibility flush both
  // talk to it, and rebuilding it per render would drop a scheduled write on the floor.
  // `mutateAsync` is stable across renders, so the closure never goes stale.
  const [engine] = useState(() =>
    createSaveEngine({
      write: (content, expectedVersion) =>
        updateBody
          .mutateAsync({ content, expected_version: expectedVersion })
          .then((result) => ({ version: result.version })),
      onState: (state, detail) => {
        setSaveState(state)
        setSaveDetail(detail ?? null)
      },
      // A stale-version 409 is not an ordinary failure: it hands the engine the server's
      // current version and body so the workspace can reconcile without losing either side.
      isConflict: (error): SaveConflict | null =>
        error instanceof DraftBodyConflictError
          ? { serverVersion: error.currentVersion, serverBody: error.serverBody }
          : null,
    }),
  )

  // The poll is the live source of truth for a running pass; when it moves, the detail
  // and the pending edit are stale, exactly as the study page treats its own poll. The
  // stage detail is in the deps because a pass moves section by section within one
  // `generating` state, and each landing is worth refetching for.
  useEffect(() => {
    if (!polledState || artifactId === null) return
    queryClient.invalidateQueries({ queryKey: draftKeys.pending(artifactId) })
    queryClient.invalidateQueries({ queryKey: draftKeys.liveSuggestion(artifactId) })
    // Address-comment passes resolve their finding only after a successful landing.
    // Refetch on the settling status frame so the card closes without a page reload.
    queryClient.invalidateQueries({ queryKey: draftKeys.comments(artifactId) })
    queryClient.invalidateQueries({ queryKey: draftKeys.plan(artifactId) })
    if (classId !== null) queryClient.invalidateQueries({ queryKey: draftKeys.sources(classId) })
  }, [polledState, polledDetail, artifactId, classId, queryClient])

  // The comment anchors: every open, quoted thread, handed to the editor's decoration
  // plugin whenever the set changes - and again when a fresh editor mounts, through
  // the ref the ready callback reads.
  const anchorThreads = useMemo<AnchorThread[]>(
    () =>
      (commentThreads.data ?? [])
        .filter((thread) => !thread.resolved && !thread.orphaned && thread.quote)
        .map((thread) => ({
          id: thread.id,
          quote: thread.quote as string,
          severity: thread.severity,
        })),
    [commentThreads.data],
  )
  const anchorThreadsRef = useRef<AnchorThread[]>([])
  useEffect(() => {
    anchorThreadsRef.current = anchorThreads
    editorRef.current?.setComments(anchorThreads)
  }, [anchorThreads])

  // A running review files comments as it looks, so the tab fills while it works; on
  // settling, the closing summary has landed in the writer conversation too.
  const wasReviewingRef = useRef(false)
  useEffect(() => {
    if (artifactId === null) return
    const was = wasReviewingRef.current
    wasReviewingRef.current = reviewRunning
    if (reviewRunning || was) {
      queryClient.invalidateQueries({ queryKey: draftKeys.comments(artifactId) })
    }
    if (was && !reviewRunning) {
      queryClient.invalidateQueries({ queryKey: draftKeys.sessions(artifactId) })
      queryClient.invalidateQueries({ queryKey: chatKeys.messages(activeWriterSessionId ?? -1) })
    }
  }, [reviewRunning, polledDetail, artifactId, activeWriterSessionId, queryClient])

  // A pass owns the document and writes it section by section, moving the body version on
  // the server. When one settles, pull the rewritten body and its new version back into
  // the editor and the engine: the editor follows the pass, and the next autosave expects
  // the version the pass produced rather than falsely conflicting against it.
  const wasPassRunningRef = useRef(false)
  useEffect(() => {
    const was = wasPassRunningRef.current
    wasPassRunningRef.current = passRunning
    if (was && !passRunning) void syncEditorFromServer()
    // syncEditorFromServer is a stable closure over refs; the transition is the trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [passRunning])

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

  const openDraftDocument = useCallback(() => setDraftDialogOpen(true), [])

  /** Open the legacy one-passage `/write` block at the editor caret. */
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
  const liveSuggestionData = liveSuggestion.data ?? null
  const [liveSuggestionOverride, setLiveSuggestionOverride] = useState<LiveDraftSuggestion | null>(
    null,
  )
  const [liveSuggestionSeen, setLiveSuggestionSeen] = useState(liveSuggestionData)
  if (liveSuggestionData !== liveSuggestionSeen) {
    setLiveSuggestionSeen(liveSuggestionData)
    setLiveSuggestionOverride(null)
  }
  const activeLiveSuggestion = liveSuggestionOverride ?? liveSuggestionData ?? null

  // The plan and source ledger make the rail wider in purpose than in pixels. Keep the
  // selected tab visible when the row scrolls instead of leaving (for example) Chat
  // active while its trigger sits beyond the right edge of a narrow rail.
  useEffect(() => {
    const selected = railTabsRef.current?.querySelector<HTMLElement>(
      '[data-state="active"], [data-active]',
    )
    selected?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }, [railTab, activeLiveSuggestion, edit, commentThreads.data?.length])

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

  const [liveSuggestionSeenId, setLiveSuggestionSeenId] = useState<number | null>(null)
  if (!edit && activeLiveSuggestion && liveSuggestionSeenId !== activeLiveSuggestion.id) {
    setLiveSuggestionSeenId(activeLiveSuggestion.id)
    setRailTab('live')
  }
  if (!activeLiveSuggestion && liveSuggestionSeenId !== null) {
    setLiveSuggestionSeenId(null)
    if (railTab === 'live') setRailTab('chat')
  }

  const loaded = draft.data ?? null
  useFullBleed(loaded !== null)
  // Only once there is a draft to be immersed in. A skeleton or an error with no header
  // and no sidebar is a blank window with nothing to click, and the stored preference
  // would put every student who ever used the mode into one on a slow load.
  useImmersiveChrome(immersive && loaded !== null)

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

  /**
   * Land the newest local body and prove the server holds it before a body-dependent
   * operation runs on it (PLA-289). The result is `ok` only when `flush` confirmed the
   * current editor text at `version`; a save failure surfaces the actionable state, and a
   * conflict opens the reconciliation dialog. Either way, the caller must not proceed unless
   * `ok`, because the operation reads the body server-side and would otherwise act on stale
   * text.
   */
  const flushBeforeAction = useCallback(async (): Promise<{ ok: boolean; version: number }> => {
    const result = await engine.flush(latestMarkdownRef.current)
    if (!result.ok && result.status === 'error') {
      toast.error(
        'Your latest writing has not been saved yet, so this was not started. Check the save state and try again.',
      )
    }
    return { ok: result.ok, version: result.version }
  }, [engine])

  const ensureBodySaved = useCallback(
    async () => (await flushBeforeAction()).ok,
    [flushBeforeAction],
  )

  /**
   * After an accept, restore, or a settled pass: reconcile the editor with the body the
   * server now holds. A server operation moved the body and its version, and the editor
   * must follow it - but never over unresolved local work.
   *
   * The order of the checks is the whole point:
   *
   * - If the editor already shows the server's body, only the version base moved; adopt it.
   * - If a write is racing this sync (in flight, or a debounce about to fire), that write
   *   carries the pre-operation version, so the server's compare-and-swap refuses it and the
   *   engine raises the conflict itself. Leave the local text and the pipeline untouched.
   * - If there is no unsaved local divergence, follow the server: reset the editor to it.
   * - Otherwise the student has unsaved text the operation moved under. Raise a conflict and
   *   keep their words; never reset over them. `noteSaved` is reached only on the safe paths.
   */
  async function syncEditorFromServer() {
    if (artifactId === null) return
    await queryClient.invalidateQueries({ queryKey: draftKeys.detail(artifactId) })
    const fresh = queryClient.getQueryData<DraftDetail>(draftKeys.detail(artifactId))
    if (!fresh) return
    const localBody = latestMarkdownRef.current
    const seeded = normalizeMathDelimiters(fresh.body)

    if (localBody === fresh.body || localBody === seeded) {
      // The editor already holds what the server has; only the version base moved forward.
      engine.noteSaved(fresh.body, fresh.body_version)
      if (localBody !== seeded) {
        latestMarkdownRef.current = seeded
        setLatestMarkdown(seeded)
        editorRef.current?.reset(seeded)
      }
      setSaveState('saved')
      if (seeded !== fresh.body) engine.schedule(seeded)
      return
    }

    const decision = decideServerSync(engine, localBody)
    if (decision === 'skip') return
    if (decision === 'adopt') {
      engine.noteSaved(fresh.body, fresh.body_version)
      latestMarkdownRef.current = seeded
      setLatestMarkdown(seeded)
      editorRef.current?.reset(seeded)
      setSaveState('saved')
      if (seeded !== fresh.body) engine.schedule(seeded)
      return
    }

    // Unsaved local writing, and the server moved under it: reconcile rather than clobber.
    engine.forceConflict(fresh.body, fresh.body_version)
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

  function onLiveSuggestionFinalized(nextEdit: PendingEdit) {
    setEditOverride(nextEdit)
    setLiveSuggestionOverride(null)
    setRailTab('suggestion')
  }

  async function onSnapshot() {
    const content = latestMarkdownRef.current
    // Land the newest words through the same pipeline first so the snapshot writes on top
    // of a known version rather than as a competing out-of-order request. If that flush did
    // not confirm the current text (a save failure or an already-open conflict), do not
    // snapshot over stale server text.
    if (!(await ensureBodySaved())) return
    try {
      const result = await updateBody.mutateAsync({
        content,
        expected_version: engine.version(),
        snapshot: true,
      })
      // The snapshot wrote the content too, so nothing is dirty any longer.
      engine.noteSaved(content, result.version)
      setSaveState('saved')
      toast.success('Snapshot saved to the history.')
    } catch (caught) {
      if (caught instanceof DraftBodyConflictError) {
        // The body moved under us between the flush and the snapshot (a second tab). Feed it
        // into the same reconciliation the autosave uses: keep the local text, drop the false
        // Saved, expose the server's version, and let the student choose - never a bare toast
        // over a still-Saved indicator, and never "reopen the draft" (PLA-289).
        engine.forceConflict(caught.serverBody, caught.currentVersion)
        return
      }
      toast.error(caught instanceof ApiError ? caught.message : 'Could not save a snapshot.')
    }
  }

  // Reconcile a stale-version conflict by keeping the student's own writing: rebase onto
  // what the server holds now and write the local text over it. Nothing on screen is lost.
  function onKeepMyVersion() {
    engine.keepLocal(latestMarkdownRef.current)
  }

  // Reconcile by taking the version saved elsewhere: adopt it as the base and load it into
  // the editor. The student chose this, having seen both, so replacing the text is honest.
  function onUseServerVersion() {
    const resolved = engine.takeServer()
    if (!resolved) return
    const seeded = normalizeMathDelimiters(resolved.serverBody)
    latestMarkdownRef.current = seeded
    setLatestMarkdown(seeded)
    editorRef.current?.reset(seeded)
    setSaveState('saved')
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
  // Bodies written before AI text was normalized server-side still hold `\(x\)` and bare
  // `\begin{align}`, which remark-math does not read - they render as literal
  // backslashes in the editor while rendering correctly in the chat pane beside it. The
  // difference is scheduled as an edit in `onEditorReady`, so the stored body converges.
  const seedBody = normalizeMathDelimiters(artifact.body)
  const state = polledState ?? artifact.state
  const stageDetail = status.data?.stage_detail ?? artifact.stage_detail
  const errorMessage = status.data?.error_message ?? artifact.error_message
  const generating = state === 'pending' || state === 'generating'
  // The same review/pass split as the poll effects above, but from the settled-state
  // fallbacks too, so a fresh visit mid-review renders the editor unlocked.
  const reviewing =
    generating &&
    (status.data?.job_kind === 'review' ||
      (status.data?.job_kind == null && (stageDetail?.startsWith('Reviewing') ?? false)))
  const className = classes.data?.find((entry) => entry.id === classId)?.name ?? 'Class'
  // Both residents run for minutes: a pass counts sections, a review counts its four
  // lenses. Without the count a stage name that sits still reads as a hang.
  const stepsTotal = status.data?.problems_total ?? null
  const stepsDone = status.data?.problems_done ?? 0
  const progress = stepsTotal ? ` (${Math.min(stepsDone + 1, stepsTotal)}/${stepsTotal})` : ''
  const cancelRequested = status.data?.cancel_requested ?? false
  const runWarnings = status.data?.warnings ?? []
  const runMetadata = [
    status.data?.depth ? depthLabel(status.data.depth) : null,
    status.data?.started_at ? elapsedLabel(status.data.started_at) : null,
  ].filter(Boolean)
  const runMetadataText = runMetadata.length > 0 ? ` · ${runMetadata.join(' · ')}` : ''

  // The two columns as values, so the rail can sit on either side of the document
  // without the JSX being written twice. Reading order stays page-first in the DOM
  // whichever side it is on: a keyboard reaches the writing before the tools.
  // The document column: centred to a reading measure, scrolling on its own. In print it
  // becomes its full height - a printed page cannot be scrolled.
  //
  // `xl:h-full` is what stops the slash menu getting clipped. Inside a resizable panel the
  // parent is not a flex container, so `flex-1` collapses this box to its content height;
  // the scroll box then hugs a short document, and the block-edit menu - positioned
  // absolutely inside the editor - is cut off at the content's bottom edge. `h-full` fills
  // the panel so the menu has the whole column to open into. `flex-1` still carries the
  // stacked mobile layout below `lg`, where the parent *is* a flex column.
  const documentPane = (
    <div className="min-h-0 flex-1 overflow-y-auto xl:h-full print:overflow-visible">
      {/* `inert` while a pass runs: the pass owns the document and the editor is a
          viewer following it - sections appear as they land. Typing into a body the
          server is rewriting would race the autosave against the pipeline, and the
          autosave writes whole documents. A review leaves the editor live, so this is a
          pass alone. */}
      <div
        inert={generating && !reviewing}
        className={cn(
          // 760px of column meant ~680px of text however wide the window: a fine prose
          // measure and a bad one for the equations and tables a technical draft is full
          // of, and on a large display it left the writing in a ribbon down the middle
          // with a third of the screen of dead gutter either side. Wider, wider again on
          // a large screen, and the split beside it is draggable for anyone who wants
          // more still.
          'mx-auto w-full max-w-[900px] px-8 py-8 md:px-12 xl:max-w-[1040px]',
        )}
      >
        <DraftEditor
          key={artifact.id}
          ref={editorRef}
          initialMarkdown={seedBody}
          onCommentClick={(commentId) => {
            setRailTab('comments')
            window.setTimeout(() => {
              const thread = document.getElementById(`comment-thread-${commentId}`)
              thread?.focus({ preventScroll: true })
              thread?.scrollIntoView({ block: 'center' })
            }, 0)
          }}
          onSourceClick={(sourceId) => {
            setRailTab('sources')
            window.setTimeout(() => {
              const source = document.getElementById(`source-${sourceId}`)
              source?.focus({ preventScroll: true })
              source?.scrollIntoView({ block: 'center' })
            }, 0)
          }}
          onChange={(markdown) => {
            latestMarkdownRef.current = markdown
            setLatestMarkdown(markdown)
            engine.schedule(markdown)
          }}
          onEditorReady={(view) => {
            // The engine starts from what the server holds, at the version it holds it, so
            // the seed document is not a change waiting to be written back.
            engine.noteSaved(artifact.body, artifact.body_version)
            latestMarkdownRef.current = seedBody
            setLatestMarkdown(seedBody)
            // A body whose math delimiters needed converting is now one edit ahead
            // of the server. Schedule that edit rather than leaving the two to
            // diverge: comment anchors and pending-edit diffs are computed
            // server-side against the stored body, and they would drift from the
            // text on screen until the student happened to type something.
            if (seedBody !== artifact.body) engine.schedule(seedBody)
            view.dom.setAttribute('aria-label', 'Draft document')
            // A fresh editor knows nothing of the comments already filed.
            editorRef.current?.setComments(anchorThreadsRef.current)
          }}
        />
      </div>
    </div>
  )

  const railPane = (
    <aside
      className={cn(
        // `xl:h-full` for the same reason the document column has it: inside a resizable
        // panel the parent is not flex, so the tools fill the panel's height rather than
        // collapsing to the tab bar - which is what keeps the chat composer at the bottom.
        'border-border flex min-h-0 basis-[45%] shrink-0 flex-col border-t xl:h-full xl:basis-auto xl:shrink xl:border-t-0 print:hidden',
        // The rail's one border is whichever edge faces the page.
        railSide === 'left' ? 'xl:border-r' : 'xl:border-l',
      )}
      aria-label="Draft tools"
    >
      <Tabs
        value={railTab}
        onValueChange={(value) => setRailTab(value as RailTab)}
        className="flex min-h-0 flex-1 flex-col gap-0"
      >
        <div
          ref={railTabsRef}
          className="border-border flex min-w-0 shrink-0 items-center gap-2 border-b p-2"
        >
          <Label htmlFor="draft-tool-picker" className="sr-only">
            Draft tool
          </Label>
          <Select value={railTab} onValueChange={(value) => setRailTab(value as RailTab)}>
            <SelectTrigger
              id="draft-tool-picker"
              size="sm"
              className="min-w-0 flex-1"
              aria-label="Draft tool"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent position="popper" align="start">
              {activeLiveSuggestion ? (
                <SelectItem value="live">Draft · Live draft</SelectItem>
              ) : null}
              <SelectItem value="plan">Draft · Plan</SelectItem>
              {edit ? <SelectItem value="suggestion">Review · Suggestion</SelectItem> : null}
              {(commentThreads.data?.length ?? 0) > 0 || reviewing || railTab === 'comments' ? (
                <SelectItem value="comments">
                  Review · Comments
                  {commentThreads.data?.length ? ` (${commentThreads.data.length})` : ''}
                </SelectItem>
              ) : null}
              <SelectItem value="sources">Research · Sources</SelectItem>
              <SelectItem value="history">Workspace · History</SelectItem>
              <SelectItem value="chat">Assistant · Chat</SelectItem>
            </SelectContent>
          </Select>
          {wide ? (
            <RailSideToggle
              side={railSide}
              className="shrink-0"
              onToggle={() => setRailSide(railSide === 'left' ? 'right' : 'left')}
            />
          ) : null}
        </div>

        {activeLiveSuggestion ? (
          <TabsContent value="live" className="min-h-0 flex-1 overflow-y-auto p-4">
            <LiveDraftSuggestionPanel
              draftId={artifact.id}
              suggestion={activeLiveSuggestion}
              onFinalized={onLiveSuggestionFinalized}
              onOpenPlan={() => setRailTab('plan')}
            />
          </TabsContent>
        ) : null}

        {edit ? (
          <TabsContent value="suggestion" className="min-h-0 flex-1 overflow-y-auto p-4">
            <SuggestionPanel
              draftId={artifact.id}
              edit={edit}
              currentBody={latestMarkdown}
              onApplied={onSuggestionApplied}
              // Land and confirm the student's own writing before the suggestion replaces
              // the body, and carry the version they reviewed against so a concurrent change
              // conflicts rather than being silently overwritten (PLA-289).
              saveBarrier={flushBeforeAction}
              onBodyConflict={(conflict) =>
                engine.forceConflict(conflict.serverBody, conflict.serverVersion)
              }
            />
          </TabsContent>
        ) : null}

        <TabsContent value="plan" className="min-h-0 flex-1 overflow-y-auto p-4">
          <PlanPanel
            draftId={artifact.id}
            running={generating || startPass.isPending}
            onRun={async () => {
              // The pass reads the body server-side; do not start it over stale text.
              if (!(await ensureBodySaved())) return
              await startPass.mutateAsync({ depth: 'standard' })
              toast.success('Lyra is continuing from the saved plan.')
            }}
          />
        </TabsContent>

        <TabsContent value="sources" className="min-h-0 flex-1 overflow-y-auto p-4">
          <SourceLedger classId={classId} />
        </TabsContent>

        <TabsContent value="comments" className="min-h-0 flex-1 overflow-y-auto p-4">
          <CommentList
            draftId={artifact.id}
            onJump={(comment) => editorRef.current?.jumpToComment(comment.id) ?? false}
          />
        </TabsContent>

        <TabsContent value="history" className="min-h-0 flex-1 overflow-y-auto p-4">
          <div className="flex flex-col gap-3">
            <p className="text-text-secondary text-sm">
              Snapshots and accepted suggestions, newest first. Restoring one writes a new version,
              so nothing is ever lost.
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
            {loaded ? (
              <>
                <BriefCard draftId={loaded.id} />
                {/* One assistant, no Guide/Show: the writer. Its turns narrate their
                    tool calls, and a proposal it lands mid-turn arrives through the
                    pending-edit query the same way a pass's does. */}
                <ChatPane
                  classId={classId}
                  className={className}
                  selectedDocumentId={null}
                  onClearSelectedDocument={() => undefined}
                  writer={{
                    artifactId: loaded.id,
                    onProposed: () => {
                      void queryClient.invalidateQueries({
                        queryKey: draftKeys.pending(loaded.id),
                      })
                    },
                    onBrief: () => {
                      void queryClient.invalidateQueries({
                        queryKey: draftKeys.brief(loaded.id),
                      })
                    },
                    onPass: () => {
                      // The assistant queued a pass mid-turn. Land the student's
                      // newest words first, then let the status poll pick it up.
                      void engine.flush(latestMarkdownRef.current)
                      void queryClient.invalidateQueries({
                        queryKey: draftKeys.status(loaded.id),
                      })
                    },
                    onReview: () => {
                      // Same flush, different reason: the review's quotes must
                      // anchor into the words as the student left them.
                      void engine.flush(latestMarkdownRef.current)
                      void queryClient.invalidateQueries({
                        queryKey: draftKeys.status(loaded.id),
                      })
                      setRailTab('comments')
                    },
                    onComments: () => {
                      // The writer replied under threads mid-turn.
                      void queryClient.invalidateQueries({
                        queryKey: draftKeys.comments(loaded.id),
                      })
                    },
                  }}
                  sessionId={activeWriterSessionId}
                  layout="inline"
                  scrollViewportRef={chatScrollRef}
                  onSessionIdChange={setWriterSessionId}
                  emptyState={
                    <p className="text-text-tertiary text-sm">
                      Talk to Lyra about this piece: what the assignment wants, what to write next,
                      or what to fix. It reads the document and the class material before it
                      answers.
                    </p>
                  }
                />
              </>
            ) : null}
          </div>
        </TabsContent>
      </Tabs>
    </aside>
  )

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
          <DraftEntryActions onDraftDocument={openDraftDocument} onDraftPassage={openWrite} />
          <Button
            variant="ghost"
            size="sm"
            disabled={generating || startReview.isPending}
            onClick={() => setReviewOpen(true)}
            title="Review the draft: structure, argument, prose, and claims"
          >
            <SearchCheck className="size-4" />
            Review
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
          {/* Export when the machine can typeset; Print as the honest fallback when it
              cannot. One slot, because both are "get this out of the app". */}
          {exportability.data?.available ? (
            <Button
              variant="ghost"
              size="sm"
              disabled={exporting}
              title="Export a typeset PDF"
              onClick={async () => {
                // The export reads the body server-side; do not export stale text. A save
                // failure or conflict leaves the export unstarted with the save state shown.
                if (!(await ensureBodySaved())) return
                setExporting(true)
                try {
                  const pdf = await api.exportDraftPdf(artifact.id)
                  const url = URL.createObjectURL(pdf)
                  const link = document.createElement('a')
                  link.href = url
                  link.download = `${artifact.title}.pdf`
                  link.click()
                  URL.revokeObjectURL(url)
                } catch (caught) {
                  toast.error(
                    caught instanceof ApiError ? caught.message : 'Could not export the PDF.',
                  )
                } finally {
                  setExporting(false)
                }
              }}
            >
              {exporting ? <Spinner className="size-4" /> : <FileDown className="size-4" />}
              Export PDF
            </Button>
          ) : (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => window.print()}
              title={exportability.data?.message ?? 'Print this draft'}
            >
              <Printer className="size-4" />
              Print
            </Button>
          )}
          {/* Last, and the only icon-only control in the row: it is the one button here
              that acts on the window rather than on the draft. It stays on screen in both
              states, so the way out of the mode is never something to go looking for. */}
          <ImmersiveToggle immersive={immersive} onToggle={() => setImmersive(!immersive)} />
        </div>
      </header>

      {generating ? (
        <div
          className="border-border bg-accent-surface/40 flex items-center justify-between gap-3 border-b px-4 py-2 text-sm md:px-6 print:hidden"
          aria-live="polite"
        >
          <div className="flex min-w-0 items-center gap-2">
            <Spinner className="size-3.5 shrink-0" />
            <span className="text-text-secondary">
              {stageDetail ? `${stageDetail}${progress}${runMetadataText}` : 'Lyra is drafting.'}
            </span>
          </div>
          <Button
            variant="outline"
            size="sm"
            disabled={cancelRequested || cancelRun.isPending}
            onClick={() => {
              void cancelRun.mutateAsync().catch((caught) => {
                toast.error(
                  caught instanceof ApiError
                    ? caught.message
                    : 'Could not request cancellation for this run.',
                )
              })
            }}
          >
            {cancelRequested || cancelRun.isPending ? (
              <>
                <Spinner className="size-3.5" />
                Canceling…
              </>
            ) : (
              'Cancel'
            )}
          </Button>
        </div>
      ) : null}

      {runWarnings.length > 0 ? (
        <Alert className="m-4 mb-0 w-auto print:hidden">
          <AlertTitle>{runWarnings.length === 1 ? 'Run note' : 'Run notes'}</AlertTitle>
          <AlertDescription>
            <ul className="list-disc space-y-1 pl-5">
              {runWarnings.map((warning) => (
                <li key={`${warning.code}:${warning.message}`}>{warning.message}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      ) : null}

      {/* What a settled pass left to say: the parked outline waiting for review, how far
          an interrupted pass got, or that it had nothing to suggest. Quiet, and gone the
          next time a pass runs. */}
      {!generating && state === 'ready' && stageDetail ? (
        <div
          className="border-border bg-muted/40 flex items-center gap-2 border-b px-4 py-2 text-sm md:px-6 print:hidden"
          aria-live="polite"
        >
          <span className="text-text-secondary">{stageDetail}</span>
        </div>
      ) : null}

      {state === 'failed' ? (
        <Alert variant="destructive" className="m-4 w-auto print:hidden">
          <AlertTitle>Lyra could not finish that run</AlertTitle>
          <AlertDescription>
            <p>{errorMessage ?? 'Something went wrong while working on it.'}</p>
            <Button variant="outline" size="sm" className="mt-3" onClick={openDraftDocument}>
              Try again
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}

      {/* Below `xl` the two stack and the rail sits under the page, because there is one
          column and the page is what it is for. Above it they share a draggable split:
          the measure was a hardcoded 760px against a fixed 380px rail, which on a wide
          window left the text in a narrow ribbon with a third of the screen of dead
          gutter either side, and gave the student no way to say otherwise. */}
      {wide ? (
        <ResizablePanelGroup
          orientation="horizontal"
          defaultLayout={railSide === 'left' ? railFirstLayout : documentFirstLayout}
          onLayoutChanged={(layout, meta) => {
            // Only a drag is a choice; the library reports its own recomputes here too.
            if (!meta.isUserInteraction) return
            const share = shareOf(layout, 'rail')
            if (share) setRailShare(Math.round(share))
          }}
          className="min-h-0 flex-1"
        >
          {/* `flex flex-col` on each panel: the library sizes its inner content wrapper
              with `flex-grow: 1`, which only fills when the panel is a flex container.
              Without it the wrapper - and everything in it - collapses to content height,
              which is what clipped the slash menu. */}
          {railSide === 'left' ? (
            <>
              <ResizablePanel id="rail" minSize="20" maxSize="45" className="flex flex-col">
                {railPane}
              </ResizablePanel>
              <ResizableHandle withHandle className="print:hidden" />
              <ResizablePanel id="document" minSize="45" className="flex flex-col">
                {documentPane}
              </ResizablePanel>
            </>
          ) : (
            <>
              <ResizablePanel id="document" minSize="45" className="flex flex-col">
                {documentPane}
              </ResizablePanel>
              <ResizableHandle withHandle className="print:hidden" />
              <ResizablePanel id="rail" minSize="20" maxSize="45" className="flex flex-col">
                {railPane}
              </ResizablePanel>
            </>
          )}
        </ResizablePanelGroup>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col">
          {documentPane}
          {railPane}
        </div>
      )}

      {bodyPart ? (
        <RevisionHistory
          artifactId={artifact.id}
          part={historyOpen ? bodyPart : null}
          noun="draft"
          // A draft's live body can be newer than the newest recorded revision (autosave
          // records none), so the history marks "shown now" by matching this, not by assuming
          // the top row is current (PLA-289).
          currentBody={latestMarkdown}
          // Confirm the current body first (so it is itself a recoverable revision) and
          // restore against its version, so a stale tab restoring cannot replace a body that
          // changed elsewhere - it conflicts and reconciles instead (PLA-289).
          saveBeforeRestore={flushBeforeAction}
          onBodyConflict={(conflict) =>
            engine.forceConflict(conflict.serverBody, conflict.serverVersion)
          }
          onClose={() => {
            setHistoryOpen(false)
            // A restore writes the body server-side, so the editor follows it.
            void syncEditorFromServer()
          }}
        />
      ) : null}

      <DraftConflictDialog
        conflict={saveState === 'conflict' ? engine.conflict() : null}
        onKeepMine={onKeepMyVersion}
        onUseServer={onUseServerVersion}
      />
      <DraftDocumentDialog
        open={draftDialogOpen}
        pending={startPass.isPending}
        onOpenChange={setDraftDialogOpen}
        onStart={async (request) => {
          // The pass reads the body server-side, so the student's newest words must be
          // confirmed on disk before the job is queued. A failed or conflicted save leaves
          // the pass unstarted with the actionable save state (PLA-289).
          if (!(await ensureBodySaved())) return
          await startPass.mutateAsync(request)
          toast.success(
            request.pause_at_plan
              ? 'Lyra is building the plan.'
              : request.instruction
                ? 'Lyra is working on it.'
                : 'Lyra is drafting the document.',
          )
        }}
      />
      <ReviewDialog
        open={reviewOpen}
        pending={startReview.isPending}
        onOpenChange={setReviewOpen}
        onStart={async (depth) => {
          // The review's quotes must anchor into the words as the student left them, so the
          // newest text must be confirmed on disk before it starts (PLA-289).
          if (!(await ensureBodySaved())) return
          await startReview.mutateAsync({ depth })
          setRailTab('comments')
          toast.success('Lyra is reviewing the draft.')
        }}
      />
    </div>
  )
}

/**
 * Give the window to the writing, or hand the application back.
 *
 * The sidebar and the header are two borders and a row of somewhere else to be, and a
 * draft is the one route where what is on screen is meant to be read as a page rather than
 * navigated. The draft's own header stays either way: it holds the save state, the four
 * things you do to a draft, and this button, so the mode is never something a student has
 * to guess their way out of. Escape is deliberately not bound to it - the editor below
 * spends Escape on its own tooltips and menus, and taking that key would break the writing
 * to save a click on a button that has not moved.
 */
function ImmersiveToggle({ immersive, onToggle }: { immersive: boolean; onToggle: () => void }) {
  const label = immersive ? 'Show the sidebar and header' : 'Hide the sidebar and header'
  return (
    <Button
      variant="ghost"
      size="icon"
      className="text-text-tertiary hover:text-text-primary size-8"
      onClick={onToggle}
      aria-pressed={immersive}
      aria-label={label}
      title={label}
    >
      {immersive ? <Minimize2 className="size-4" /> : <Maximize2 className="size-4" />}
    </Button>
  )
}

/** Send the tools to the other side of the page, and keep them there. */
function RailSideToggle({
  side,
  className,
  onToggle,
}: {
  side: RailSide
  className?: string
  onToggle: () => void
}) {
  const label = side === 'left' ? 'Move the tools to the right' : 'Move the tools to the left'
  return (
    <Button
      variant="ghost"
      size="icon"
      className={cn('text-text-tertiary hover:text-text-primary size-7 self-center', className)}
      onClick={onToggle}
      aria-label={label}
      title={label}
    >
      {side === 'left' ? (
        <PanelRightClose className="size-3.5" />
      ) : (
        <PanelLeftClose className="size-3.5" />
      )}
    </Button>
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

/**
 * The stale-version reconciliation. A second tab, a slow retry, or an AI pass moved the
 * saved draft past what this editor last knew, so a save was refused rather than allowed to
 * overwrite newer text. Both versions are intact: the student's writing is still in the
 * editor, and the version saved elsewhere is shown here. The choice is theirs, and the
 * dialog stays until they make it - there is no silent reload over their words.
 */
function DraftConflictDialog({
  conflict,
  onKeepMine,
  onUseServer,
}: {
  conflict: SaveConflict | null
  onKeepMine: () => void
  onUseServer: () => void
}) {
  return (
    <Dialog open={conflict !== null}>
      <DialogContent className="max-w-xl" showCloseButton={false}>
        <DialogHeader>
          <DialogTitle>This draft was changed somewhere else</DialogTitle>
          <DialogDescription>
            Your latest writing was not saved, because a newer version of this draft was saved from
            another place - likely this draft open in a second tab. Nothing was lost. Your writing
            is still right here in the editor, and the other version is below. Choose which one to
            keep.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-2">
          <p className="text-text-secondary text-sm font-medium">The version saved elsewhere</p>
          <pre className="border-border bg-muted max-h-64 overflow-auto rounded-md border p-3 text-xs whitespace-pre-wrap">
            {conflict?.serverBody === '' ? '(empty)' : conflict?.serverBody}
          </pre>
          <p className="text-text-tertiary text-xs">
            Keeping your writing replaces the version above with what is in your editor. Using the
            other version replaces your editor with the text above.
          </p>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onUseServer}>
            Use the other version
          </Button>
          <Button onClick={onKeepMine}>Keep what I wrote</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** The instruction a whole-document suggestion pass starts from. */
function DraftDocumentDialog({
  open,
  pending,
  onOpenChange,
  onStart,
}: {
  open: boolean
  pending: boolean
  onOpenChange: (open: boolean) => void
  /** Starts the pass; an empty instruction is the full draft-the-document pass. */
  onStart: (request: PassRequest) => Promise<void>
}) {
  const instructionId = useId()
  const [instruction, setInstruction] = useState('')
  const [depth, setDepth] = useState<WriterDepth>('standard')
  const [pauseAtPlan, setPauseAtPlan] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [openSeen, setOpenSeen] = useState(open)
  if (open !== openSeen) {
    setOpenSeen(open)
    setInstruction('')
    setDepth('standard')
    setPauseAtPlan(false)
    setError(null)
  }

  async function submit() {
    try {
      await onStart({
        instruction: instruction.trim() || null,
        depth,
        pause_at_plan: pauseAtPlan,
      })
      onOpenChange(false)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not start the pass.')
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Draft the document</DialogTitle>
          <DialogDescription>
            Lyra researches and plans first, outlines every paragraph, drafts each block, then
            reviews transitions and the complete piece. The live draft stays separate from your
            document until you review and accept it.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-2">
          <Label htmlFor={instructionId}>What should Lyra draft?</Label>
          <Input
            id={instructionId}
            value={instruction}
            autoFocus
            autoComplete="off"
            placeholder="For example: Write a complete five-page paper"
            onChange={(event) => setInstruction(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                void submit()
              }
            }}
          />
        </div>
        <DepthField id="pass-depth" value={depth} onChange={setDepth} />
        <div className="flex items-start justify-between gap-4">
          <div>
            <Label htmlFor="pause-at-plan">Pause after the plan</Label>
            <p className="text-text-tertiary text-sm">
              Stop before drafting so you can edit the thesis and section jobs in the Plan tab.
            </p>
          </div>
          <Switch id="pause-at-plan" checked={pauseAtPlan} onCheckedChange={setPauseAtPlan} />
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
          <Button disabled={pending} onClick={() => void submit()}>
            {pending ? <Spinner /> : null}
            Start staged draft
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ReviewDialog({
  open,
  pending,
  onOpenChange,
  onStart,
}: {
  open: boolean
  pending: boolean
  onOpenChange: (open: boolean) => void
  onStart: (depth: WriterDepth) => Promise<void>
}) {
  const [depth, setDepth] = useState<WriterDepth>('standard')
  const [error, setError] = useState<string | null>(null)
  const [openSeen, setOpenSeen] = useState(open)
  if (open !== openSeen) {
    setOpenSeen(open)
    setDepth('standard')
    setError(null)
  }

  async function submit() {
    try {
      await onStart(depth)
      onOpenChange(false)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not start the review.')
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Review the draft</DialogTitle>
          <DialogDescription>
            Lyra checks structure, argument, prose, and claims, then pins each finding to the
            passage it is about.
          </DialogDescription>
        </DialogHeader>
        <DepthField id="review-depth" value={depth} onChange={setDepth} />
        {error ? (
          <p className="text-danger-text text-sm" role="alert">
            {error}
          </p>
        ) : null}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={pending} onClick={() => void submit()}>
            {pending ? <Spinner /> : null}
            Start review
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function DepthField({
  id,
  value,
  onChange,
}: {
  id: string
  value: WriterDepth
  onChange: (depth: WriterDepth) => void
}) {
  const descriptions: Record<WriterDepth, string> = {
    quick: 'A light pass close to today’s speed.',
    standard: 'A balanced process with critique and revision.',
    deep: 'The largest research and revision budget; it may take an hour or more.',
  }
  return (
    <div className="grid gap-2">
      <Label htmlFor={id}>Depth</Label>
      <Select value={value} onValueChange={(next) => onChange(next as WriterDepth)}>
        <SelectTrigger id={id} className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="quick">Quick</SelectItem>
          <SelectItem value="standard">Standard</SelectItem>
          <SelectItem value="deep">Deep</SelectItem>
        </SelectContent>
      </Select>
      <p className="text-text-tertiary text-sm">{descriptions[value]}</p>
    </div>
  )
}

function depthLabel(depth: WriterDepth): string {
  return `${depth[0].toUpperCase()}${depth.slice(1)} depth`
}

function elapsedLabel(startedAt: string): string | null {
  const timestamp = Date.parse(startedAt)
  if (!Number.isFinite(timestamp)) return null
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000))
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  return minutes > 0 ? `${minutes}m ${remainder}s elapsed` : `${remainder}s elapsed`
}
