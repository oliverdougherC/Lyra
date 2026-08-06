'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'
import { classKeys } from '@/lib/hooks/use-classes'
import { solutionKeys } from '@/lib/hooks/use-solutions'
import { isGenerating } from '@/lib/hooks/use-study'
import type { ArtifactState, DraftBodyUpdate } from '@/types'

export const draftKeys = {
  list: (classId: number) => ['drafts', classId] as const,
  detail: (draftId: number) => ['draft', draftId] as const,
  status: (draftId: number) => ['draft-status', draftId] as const,
  pending: (draftId: number) => ['draft-pending', draftId] as const,
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
 */
function draftPollInterval(query: {
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
 * Queues a whole-document suggestion pass. The status poll is what watches it work, so
 * starting one invalidates the status; the pending edit is refetched when the run settles.
 */
export function useSuggest(draftId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (instruction: string) => api.suggestDraft(draftId, instruction),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: draftKeys.status(draftId) })
      queryClient.invalidateQueries({ queryKey: draftKeys.detail(draftId) })
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
