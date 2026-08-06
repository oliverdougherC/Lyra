'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'
import type { SettingsUpdate } from '@/types'

export const settingsKeys = {
  all: ['settings'] as const,
  models: ['settings', 'models'] as const,
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
