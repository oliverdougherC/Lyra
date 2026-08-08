'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'
import { classKeys } from '@/lib/hooks/use-classes'
import { solutionKeys } from '@/lib/hooks/use-solutions'
import { isGenerating } from '@/lib/hooks/use-study'
import type {
  ArtifactState,
  BriefWrite,
  DraftBodyUpdate,
  DraftPlanUpdate,
  DraftStatus,
  PassRequest,
  ReviewRequest,
} from '@/types'

export const draftKeys = {
  list: (classId: number) => ['drafts', classId] as const,
  detail: (draftId: number) => ['draft', draftId] as const,
  status: (draftId: number) => ['draft-status', draftId] as const,
  pending: (draftId: number) => ['draft-pending', draftId] as const,
  brief: (draftId: number) => ['draft-brief', draftId] as const,
  sessions: (draftId: number) => ['draft-sessions', draftId] as const,
  comments: (draftId: number) => ['draft-comments', draftId] as const,
  plan: (draftId: number) => ['draft-plan', draftId] as const,
  sources: (classId: number) => ['draft-sources', classId] as const,
}

/** How often the list asks again while a suggestion run is on any draft in it. */
const IN_FLIGHT_POLL_MS = 1500

/**
 * How often to ask for the list again, given what the list currently says.
 *
 * A suggestion run is a background job on the server, so the only way the panel learns a
 * draft moved from `generating` back to `ready` is to ask - the same derivation the study
 * list makes for decks and quizzes.
 */
function draftListPollInterval(list: { state: ArtifactState }[] | undefined): number | false {
  const inFlight = list?.some((draft) => isGenerating(draft.state)) ?? false
  return inFlight ? IN_FLIGHT_POLL_MS : false
}

export function useDrafts(classId: number, enabled = true) {
  return useQuery({
    queryKey: draftKeys.list(classId),
    queryFn: ({ signal }) => api.listDrafts(classId, signal),
    enabled: enabled && Number.isFinite(classId),
    refetchInterval: (query) => draftListPollInterval(query.state.data),
    refetchIntervalInBackground: true,
  })
}

export function useDraft(draftId: number, enabled = true) {
  return useQuery({
    queryKey: draftKeys.detail(draftId),
    queryFn: ({ signal }) => api.getDraft(draftId, signal),
    enabled: enabled && Number.isFinite(draftId),
  })
}

/**
 * The suggestion-run poll, copied from the study poll: 500ms at first so a fresh run
 * feels immediate, +250ms per poll, capped at 2s, and stopped the moment the artifact
 * leaves `pending` or `generating`.
 *
 * Nothing restarts this once it returns false, which is what made a job that queued
 * without marking its artifact pending invisible: the first poll saw `ready` and that
 * was the end of it. Exported so that contract can be tested directly.
 */
export function draftPollInterval(query: {
  state: { data: { state: ArtifactState } | undefined; dataUpdateCount: number }
}): number | false {
  const status = query.state.data
  if (!status) return 500
  if (!isGenerating(status.state)) return false
  return Math.min(500 + query.state.dataUpdateCount * 250, 2000)
}

export function useDraftStatus(draftId: number, enabled = true) {
  return useQuery({
    queryKey: draftKeys.status(draftId),
    queryFn: ({ signal }) => api.getDraftStatus(draftId, signal),
    enabled: enabled && Number.isFinite(draftId),
    refetchInterval: draftPollInterval,
    // A pass keeps landing sections while the student is off in another window, and
    // the editor follows this poll: without background refetches they would come back
    // to a strip and a document frozen at whatever the last focused moment saw.
    refetchIntervalInBackground: true,
  })
}

/**
 * The suggestion waiting on one draft, or null. The workspace invalidates this when the
 * status poll settles, which is when a fresh proposal has landed.
 */
export function usePendingEdit(draftId: number, enabled = true) {
  return useQuery({
    queryKey: draftKeys.pending(draftId),
    queryFn: ({ signal }) => api.getPendingEdit(draftId, signal),
    enabled: enabled && Number.isFinite(draftId),
  })
}

export function useCreateDraft(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (title: string) => api.createDraft(classId, title),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: draftKeys.list(classId) })
      queryClient.invalidateQueries({ queryKey: classKeys.all })
    },
  })
}

export function useRenameDraft(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ draftId, title }: { draftId: number; title: string }) =>
      api.renameDraft(draftId, title),
    onSuccess: (draft) => {
      queryClient.invalidateQueries({ queryKey: draftKeys.list(classId) })
      queryClient.invalidateQueries({ queryKey: draftKeys.detail(draft.id) })
    },
  })
}

export function useDeleteDraft(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (draftId: number) => api.deleteDraft(draftId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: draftKeys.list(classId) })
    },
  })
}

/**
 * The autosave mutation, and deliberately not an invalidating one: refetching the detail
 * under the writer's cursor would reset the editor they are typing in. A snapshot is a
 * history point, so it alone invalidates the revision list.
 */
export function useUpdateBody(draftId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: DraftBodyUpdate) => api.updateDraftBody(draftId, body),
    onSuccess: (result, body) => {
      if (body.snapshot) {
        queryClient.invalidateQueries({
          queryKey: solutionKeys.revisions(draftId, result.part_id),
        })
      }
    },
  })
}

/**
 * Queues a draft pass: the full draft with no arguments, or an instruction-lens pass,
 * optionally filtered to sections. The status poll is what watches it work, so
 * starting one invalidates the status; the pending edit is refetched when the run
 * settles.
 */
export function useStartPass(draftId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: PassRequest = {}) => api.startDraftPass(draftId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: draftKeys.status(draftId) })
      queryClient.invalidateQueries({ queryKey: draftKeys.detail(draftId) })
    },
  })
}

/**
 * Queues the review pass. The status poll watches it work; the workspace refetches the
 * comments as the lens details move, which is how findings appear while the review is
 * still going.
 */
export function useStartReview(draftId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: ReviewRequest = {}) => api.startReview(draftId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: draftKeys.status(draftId) })
    },
  })
}

/**
 * Ask the active pass or review to stop at its next safe boundary. The returned status is
 * cached immediately so the workspace can switch to "Canceling…" before the next poll.
 */
export function useCancelDraftRun(draftId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.cancelDraftRun(draftId),
    onSuccess: (status: DraftStatus) => {
      queryClient.setQueryData(draftKeys.status(draftId), status)
      queryClient.invalidateQueries({ queryKey: draftKeys.status(draftId) })
      queryClient.invalidateQueries({ queryKey: draftKeys.detail(draftId) })
      queryClient.invalidateQueries({ queryKey: draftKeys.pending(draftId) })
      queryClient.invalidateQueries({ queryKey: draftKeys.comments(draftId) })
    },
  })
}

/** The newest saved planning artifact for this draft, or null before planning runs. */
export function useDraftPlan(draftId: number, enabled = true) {
  return useQuery({
    queryKey: draftKeys.plan(draftId),
    queryFn: ({ signal }) => api.getDraftPlan(draftId, signal),
    enabled: enabled && Number.isFinite(draftId),
  })
}

/** Student edits create a new plan version server-side and replace the cached current plan. */
export function useUpdateDraftPlan(draftId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: DraftPlanUpdate) => api.updateDraftPlan(draftId, body),
    onSuccess: (plan) => {
      queryClient.setQueryData(draftKeys.plan(draftId), plan)
      queryClient.invalidateQueries({ queryKey: draftKeys.detail(draftId) })
    },
  })
}

/** Course documents and fetched web pages in the class's shared source ledger. */
export function useDraftSources(classId: number, enabled = true) {
  return useQuery({
    queryKey: draftKeys.sources(classId),
    queryFn: ({ signal }) => api.listDraftSources(classId, signal),
    enabled: enabled && Number.isFinite(classId),
  })
}

/**
 * The draft's comment threads, anchored server-side against the current body.
 *
 * `polling` while a review runs: findings are committed one at a time as the reviewer
 * files them, and the workspace's own invalidation is keyed off the stage detail moving.
 * A lens that spends several minutes on one section moves nothing, so this keeps the tab
 * filling underneath it.
 */
export function useComments(draftId: number, enabled = true, polling = false) {
  return useQuery({
    queryKey: draftKeys.comments(draftId),
    queryFn: ({ signal }) => api.listComments(draftId, signal),
    enabled: enabled && Number.isFinite(draftId),
    refetchInterval: polling ? 2_000 : false,
    refetchIntervalInBackground: true,
  })
}

/** The student's reply under one thread. The writer replies through its own tool. */
export function useReplyToComment(draftId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ commentId, body }: { commentId: number; body: string }) =>
      api.replyToComment(commentId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: draftKeys.comments(draftId) })
    },
  })
}

/** Resolve or reopen one thread - the student's gesture, root only. */
export function useResolveComment(draftId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ commentId, resolved }: { commentId: number; resolved: boolean }) =>
      api.resolveComment(commentId, resolved),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: draftKeys.comments(draftId) })
    },
  })
}

/**
 * Whether PDF export can run on this machine. Asked once per session: binaries do not
 * appear mid-flight, and a student who just installed one can reload.
 */
export function useExportAvailability() {
  return useQuery({
    queryKey: ['export-availability'],
    queryFn: ({ signal }) => api.getExportAvailability(signal),
    staleTime: Number.POSITIVE_INFINITY,
  })
}

/** The draft's brief, or null before one has been proposed. */
export function useBrief(draftId: number, enabled = true) {
  return useQuery({
    queryKey: draftKeys.brief(draftId),
    queryFn: ({ signal }) => api.getBrief(draftId, signal),
    enabled: enabled && Number.isFinite(draftId),
  })
}

/** The student's own edit of the brief, which lands confirmed. */
export function useSaveBrief(draftId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: BriefWrite) => api.putBrief(draftId, body),
    onSuccess: (brief) => {
      queryClient.setQueryData(draftKeys.brief(draftId), brief)
    },
  })
}

export function useConfirmBrief(draftId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.confirmBrief(draftId),
    onSuccess: (brief) => {
      queryClient.setQueryData(draftKeys.brief(draftId), brief)
    },
  })
}

/**
 * The draft's writer conversations, newest first. The rail opens the newest; older
 * ones are reachable the way any conversation is - by their transcript - once a
 * conversation switcher earns its place here.
 */
export function useWriterSessions(draftId: number, enabled = true) {
  return useQuery({
    queryKey: draftKeys.sessions(draftId),
    queryFn: ({ signal }) => api.listWriterSessions(draftId, signal),
    enabled: enabled && Number.isFinite(draftId),
  })
}

export function useCreateWriterSession(draftId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.createWriterSession(draftId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: draftKeys.sessions(draftId) })
    },
  })
}

/** One hunk echoed back to the server, pinned by its hash against races. */
export type HunkRef = { index: number; hash: string }

/**
 * Every accept writes the draft body (each one is a revision), so the detail and the
 * pending edit are both stale the moment one lands. The workspace resets the editor from
 * the refetched detail.
 */
export function useAcceptEdit(draftId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ editId, hunk, force }: { editId: number; hunk?: HunkRef; force?: boolean }) =>
      api.acceptPendingEdit(editId, { hunk, force }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: draftKeys.detail(draftId) })
      queryClient.invalidateQueries({ queryKey: draftKeys.pending(draftId) })
      queryClient.invalidateQueries({ queryKey: ['solution-revisions', draftId] })
    },
  })
}

/** A reject never writes the document, so only the pending edit goes stale. */
export function useRejectEdit(draftId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ editId, hunk }: { editId: number; hunk?: HunkRef }) =>
      api.rejectPendingEdit(editId, hunk),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: draftKeys.detail(draftId) })
      queryClient.invalidateQueries({ queryKey: draftKeys.pending(draftId) })
    },
  })
}
