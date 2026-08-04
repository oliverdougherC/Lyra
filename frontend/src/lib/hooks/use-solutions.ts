'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'
import { classKeys } from '@/lib/hooks/use-classes'
import type { SegmentationUpdate, SolutionCreate, SolutionState } from '@/types'

export const solutionKeys = {
  list: (classId: number) => ['solutions', classId] as const,
  detail: (artifactId: number) => ['solution', artifactId] as const,
  status: (artifactId: number) => ['solution-status', artifactId] as const,
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

/**
 * Polls a run's progress, backing off from 500ms to 2s and stopping once nothing is
 * working, matching the ingestion poll. Segmentation is a model pass over a whole
 * document, so the interval matters less than the stop condition: a run left at the gate
 * would otherwise be polled for as long as the tab stays open.
 */
export function useSolutionStatus(artifactId: number, enabled = true) {
  return useQuery({
    queryKey: solutionKeys.status(artifactId),
    queryFn: ({ signal }) => api.getSolutionStatus(artifactId, signal),
    enabled: enabled && Number.isFinite(artifactId),
    refetchInterval: (query) => {
      const state = query.state.data?.state
      if (state && isSettled(state)) return false
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
