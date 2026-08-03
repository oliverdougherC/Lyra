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
