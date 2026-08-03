'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'
import { classKeys } from '@/lib/hooks/use-classes'

export const chatKeys = {
  sessions: (classId: number) => ['sessions', classId] as const,
  messages: (sessionId: number) => ['messages', sessionId] as const,
}

export function useSessions(classId: number) {
  return useQuery({
    queryKey: chatKeys.sessions(classId),
    queryFn: ({ signal }) => api.listSessions(classId, signal),
    enabled: Number.isFinite(classId),
  })
}

export function useMessages(sessionId: number | null) {
  return useQuery({
    queryKey: chatKeys.messages(sessionId ?? -1),
    queryFn: ({ signal }) => api.listMessages(sessionId as number, signal),
    enabled: sessionId !== null,
  })
}

export function useCreateSession(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.createSession(classId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: chatKeys.sessions(classId) })
      queryClient.invalidateQueries({ queryKey: classKeys.all })
    },
  })
}
