'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'
import type { ClassWriterSettingsUpdate, SettingsUpdate } from '@/types'

export const settingsKeys = {
  all: ['settings'] as const,
  models: ['settings', 'models'] as const,
  writerClass: (classId: number) => ['settings', 'writer-class', classId] as const,
  desktopImport: ['settings', 'desktop-import'] as const,
}

export function useSettings() {
  return useQuery({
    queryKey: settingsKeys.all,
    queryFn: ({ signal }) => api.getSettings(signal),
  })
}

export function useUpdateSettings() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: SettingsUpdate) => api.updateSettings(body),
    onSuccess: (settings) => queryClient.setQueryData(settingsKeys.all, settings),
  })
}

export function useClassWriterSettings(classId: number) {
  return useQuery({
    queryKey: settingsKeys.writerClass(classId),
    queryFn: ({ signal }) => api.getClassWriterSettings(classId, signal),
    enabled: Number.isFinite(classId),
  })
}

export function useUpdateClassWriterSettings() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ classId, body }: { classId: number; body: ClassWriterSettingsUpdate }) =>
      api.updateClassWriterSettings(classId, body),
    onSuccess: (updated, { classId }) => {
      queryClient.setQueryData(settingsKeys.writerClass(classId), updated)
    },
  })
}

export function useTestConnection() {
  return useMutation({ mutationFn: () => api.testConnection() })
}

/**
 * Probes whether the endpoint can run tool calls. The backend records the answer, so the
 * settings query is invalidated: the stored result is what every later solve reads.
 */
export function useTestTools() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.testTools(),
    onSettled: () => queryClient.invalidateQueries({ queryKey: settingsKeys.all }),
  })
}

/**
 * Probes whether the endpoint can read an image. Stored the same way tool support is, and
 * for the same reason: a document row decides whether to offer text recognition from the
 * stored answer rather than by asking the endpoint every time it renders.
 */
export function useTestVision() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.testVision(),
    onSettled: () => queryClient.invalidateQueries({ queryKey: settingsKeys.all }),
  })
}

export function useTestExa() {
  return useMutation({ mutationFn: () => api.testExa() })
}

export function useDesktopImportStatus() {
  return useQuery({
    queryKey: settingsKeys.desktopImport,
    queryFn: ({ signal }) => api.getDesktopImportStatus(signal),
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status && ['queued', 'running', 'cancel_requested'].includes(status) ? 500 : false
    },
  })
}

export function usePreviewDesktopImport() {
  return useMutation({
    mutationFn: (selectionToken: string) => api.previewDesktopImport(selectionToken),
  })
}

export function useStartDesktopImport() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      selectionToken,
      operationId,
    }: {
      selectionToken: string
      operationId: string
    }) => api.startDesktopImport(selectionToken, operationId),
    onSuccess: (status) => queryClient.setQueryData(settingsKeys.desktopImport, status),
  })
}

export function useCancelDesktopImport() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.cancelDesktopImport(),
    onSuccess: (status) => queryClient.setQueryData(settingsKeys.desktopImport, status),
  })
}
