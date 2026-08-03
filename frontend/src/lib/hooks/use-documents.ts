'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'
import { classKeys } from '@/lib/hooks/use-classes'
import type { DocumentState } from '@/types'

export const documentKeys = {
  list: (classId: number) => ['documents', classId] as const,
  status: (documentId: number) => ['status', documentId] as const,
}

const TERMINAL_STATES: readonly DocumentState[] = ['ready', 'failed', 'unsupported']

export function isTerminal(state: DocumentState): boolean {
  return TERMINAL_STATES.includes(state)
}

export function useDocuments(
  classId: number,
  options: { enabled?: boolean; refetchInterval?: number | false } = {},
) {
  return useQuery({
    queryKey: documentKeys.list(classId),
    queryFn: ({ signal }) => api.listDocuments(classId, signal),
    enabled: options.enabled ?? Number.isFinite(classId),
    refetchInterval: options.refetchInterval ?? false,
  })
}

/**
 * Polls a single document's ingestion progress. The interval backs off from 500ms to 2s
 * so an early upload feels immediate without hammering the backend through a long
 * `extracting` stage, and stops entirely on a terminal state.
 */
export function useDocumentStatus(documentId: number, enabled = true) {
  return useQuery({
    queryKey: documentKeys.status(documentId),
    queryFn: ({ signal }) => api.getDocumentStatus(documentId, signal),
    enabled: enabled && Number.isFinite(documentId),
    refetchInterval: (query) => {
      const state = query.state.data?.state
      if (state && isTerminal(state)) return false
      const polls = query.state.dataUpdateCount
      return Math.min(500 + polls * 250, 2000)
    },
  })
}

export function useUploadDocument(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (file: File) => api.uploadDocument(classId, file),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: documentKeys.list(classId) })
      queryClient.invalidateQueries({ queryKey: classKeys.all })
    },
  })
}

export function useReingestDocument(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (documentId: number) => api.reingestDocument(documentId),
    onSuccess: (document) => {
      queryClient.invalidateQueries({ queryKey: documentKeys.list(classId) })
      queryClient.invalidateQueries({ queryKey: documentKeys.status(document.id) })
    },
  })
}

export function useDeleteDocument(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (documentId: number) => api.deleteDocument(documentId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: documentKeys.list(classId) })
      queryClient.invalidateQueries({ queryKey: classKeys.all })
    },
  })
}
