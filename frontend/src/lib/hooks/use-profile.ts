'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'

export const profileKeys = {
  forClass: (classId: number) => ['profile', classId] as const,
  user: ['profile', 'user'] as const,
}

export function useClassProfile(classId: number, enabled = true) {
  return useQuery({
    queryKey: profileKeys.forClass(classId),
    queryFn: ({ signal }) => api.getClassProfile(classId, signal),
    enabled: enabled && Number.isFinite(classId),
  })
}

export function useCorrectFact(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ factId, value }: { factId: number; value: string }) =>
      api.correctClassFact(classId, factId, value),
    onSuccess: (profile) => queryClient.setQueryData(profileKeys.forClass(classId), profile),
  })
}

export function useResolveFact(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ factId, action }: { factId: number; action: 'confirm' | 'reject' }) =>
      api.resolveClassFact(classId, factId, action),
    onSuccess: (profile) => queryClient.setQueryData(profileKeys.forClass(classId), profile),
  })
}
