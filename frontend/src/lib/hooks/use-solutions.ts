'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'
import { classKeys } from '@/lib/hooks/use-classes'
import type { PartStatus, SegmentationUpdate, SolutionCreate, SolutionState } from '@/types'

export const solutionKeys = {
  list: (classId: number) => ['solutions', classId] as const,
  detail: (artifactId: number) => ['solution', artifactId] as const,
  status: (artifactId: number) => ['solution-status', artifactId] as const,
  revisions: (artifactId: number, partId: number) =>
    ['solution-revisions', artifactId, partId] as const,
}

/**
 * States nothing is working in. `awaiting_review` is one of them: the run has stopped and
 * is waiting on the student, so polling it would be asking the same question forever.
 */
const SETTLED_STATES: readonly SolutionState[] = ['awaiting_review', 'ready', 'failed', 'cancelled']

export function isSettled(state: SolutionState): boolean {
  return SETTLED_STATES.includes(state)
}

export function useSolutions(classId: number, enabled = true) {
  return useQuery({
    queryKey: solutionKeys.list(classId),
    queryFn: ({ signal }) => api.listSolutions(classId, signal),
    enabled: enabled && Number.isFinite(classId),
  })
}

export function useSolution(artifactId: number, enabled = true) {
  return useQuery({
    queryKey: solutionKeys.detail(artifactId),
    queryFn: ({ signal }) => api.getSolution(artifactId, signal),
    enabled: enabled && Number.isFinite(artifactId),
  })
}

/** Part statuses that mean the worker is still on this problem. */
const RUNNING_PART_STATUSES: readonly PartStatus[] = ['solving', 'verifying']

/**
 * Polls a run's progress, backing off from 500ms to 2s and stopping once nothing is
 * working, matching the ingestion poll. Segmentation is a model pass over a whole
 * document, so the interval matters less than the stop condition: a run left at the gate
 * would otherwise be polled for as long as the tab stays open.
 *
 * The artifact's own state is not the whole answer. Re-solving one problem deliberately
 * leaves the artifact `ready`, because the rest of the document is still readable, so the
 * parts have to be checked too or the new solution would never arrive.
 */
export function useSolutionStatus(artifactId: number, enabled = true) {
  return useQuery({
    queryKey: solutionKeys.status(artifactId),
    queryFn: ({ signal }) => api.getSolutionStatus(artifactId, signal),
    enabled: enabled && Number.isFinite(artifactId),
    refetchInterval: (query) => {
      const status = query.state.data
      if (!status) return 500
      const working =
        !isSettled(status.state) ||
        status.parts.some((part) => RUNNING_PART_STATUSES.includes(part.status))
      if (!working) return false
      const polls = query.state.dataUpdateCount
      return Math.min(500 + polls * 250, 2000)
    },
  })
}

export function useCreateSolution(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: SolutionCreate) => api.createSolution(classId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: solutionKeys.list(classId) })
      queryClient.invalidateQueries({ queryKey: classKeys.all })
    },
  })
}

export function useUpdateSegmentation(artifactId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: SegmentationUpdate) => api.updateSegmentation(artifactId, body),
    onSuccess: (solution) => {
      // Seeded rather than invalidated: the PATCH already returned the whole artifact, so
      // refetching it would show the list the student just wrote, one round trip later.
      queryClient.setQueryData(solutionKeys.detail(artifactId), solution)
      queryClient.invalidateQueries({ queryKey: solutionKeys.status(artifactId) })
    },
  })
}

export function useStartSolution(artifactId: number, classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.startSolution(artifactId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: solutionKeys.status(artifactId) })
      queryClient.invalidateQueries({ queryKey: solutionKeys.list(classId) })
    },
  })
}

export function useUpdatePart(artifactId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ partId, content }: { partId: number; content: string }) =>
      api.updateSolutionPart(artifactId, partId, content),
    onSuccess: (_part, { partId }) => {
      queryClient.invalidateQueries({ queryKey: solutionKeys.detail(artifactId) })
      queryClient.invalidateQueries({ queryKey: solutionKeys.revisions(artifactId, partId) })
    },
  })
}

/**
 * Re-solves one problem. The status query is invalidated as well as the detail, because
 * the part goes back to `solving` and the poll is what brings the new solution in.
 */
export function useRegeneratePart(artifactId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ partId, correction }: { partId: number; correction: string }) =>
      api.regenerateSolutionPart(artifactId, partId, correction),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: solutionKeys.detail(artifactId) })
      queryClient.invalidateQueries({ queryKey: solutionKeys.status(artifactId) })
    },
  })
}

export function usePartRevisions(artifactId: number, partId: number | null) {
  return useQuery({
    queryKey: solutionKeys.revisions(artifactId, partId ?? -1),
    queryFn: ({ signal }) => api.listPartRevisions(artifactId, partId as number, signal),
    enabled: partId !== null && Number.isFinite(artifactId),
  })
}

export function useRestoreRevision(artifactId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ partId, revision }: { partId: number; revision: number }) =>
      api.restorePartRevision(artifactId, partId, revision),
    onSuccess: (_part, { partId }) => {
      queryClient.invalidateQueries({ queryKey: solutionKeys.detail(artifactId) })
      queryClient.invalidateQueries({ queryKey: solutionKeys.revisions(artifactId, partId) })
    },
  })
}

export function useResegmentSolution(artifactId: number, classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.resegmentSolution(artifactId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: solutionKeys.detail(artifactId) })
      queryClient.invalidateQueries({ queryKey: solutionKeys.status(artifactId) })
      queryClient.invalidateQueries({ queryKey: solutionKeys.list(classId) })
    },
  })
}

export function useCancelSolution(artifactId: number, classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.cancelSolution(artifactId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: solutionKeys.status(artifactId) })
      queryClient.invalidateQueries({ queryKey: solutionKeys.list(classId) })
    },
  })
}

export function useDeleteSolution(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (artifactId: number) => api.deleteSolution(artifactId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: solutionKeys.list(classId) })
    },
  })
}
