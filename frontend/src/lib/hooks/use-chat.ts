'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'
import { classKeys } from '@/lib/hooks/use-classes'

export const chatKeys = {
  sessions: (classId: number) => ['sessions', classId] as const,
  messages: (sessionId: number) => ['messages', sessionId] as const,
}

export function useSessions(classId: number | null) {
  return useQuery({
    queryKey: chatKeys.sessions(classId ?? -1),
    queryFn: ({ signal }) => api.listSessions(classId as number, signal),
    enabled: classId !== null,
  })
}

export function useMessages(sessionId: number | null) {
  return useQuery({
    queryKey: chatKeys.messages(sessionId ?? -1),
    queryFn: ({ signal }) => api.listMessages(sessionId as number, signal),
    enabled: sessionId !== null,
  })
}

export function useCreateSession(classId: number | null) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.createSession(classId as number),
    onSuccess: () => {
      if (classId !== null) {
        queryClient.invalidateQueries({ queryKey: chatKeys.sessions(classId) })
      }
      queryClient.invalidateQueries({ queryKey: classKeys.all })
    },
  })
}
