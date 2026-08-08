'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { AgentChatError, api } from '@/lib/api'
import { chatKeys } from '@/lib/hooks/use-chat'
import { draftKeys } from '@/lib/hooks/use-drafts'
import { profileKeys } from '@/lib/hooks/use-profile'
import type { AgentWorkspaceGrantsUpdate } from '@/types'
import type { AgentProfile } from '@/types'

export const agentKeys = {
  workspace: (classId: number) => ['agent', 'workspace', classId] as const,
  activity: (classId: number, sessionId: number) =>
    ['agent', 'activity', classId, sessionId] as const,
  changes: (classId: number, sessionId: number) =>
    ['agent', 'changes', classId, sessionId] as const,
  commands: (classId: number, sessionId: number) =>
    ['agent', 'commands', classId, sessionId] as const,
}

async function invalidateAgentTurnCaches(
  queryClient: ReturnType<typeof useQueryClient>,
  classId: number,
  sessionId: number,
) {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: chatKeys.messages(sessionId) }),
    queryClient.invalidateQueries({ queryKey: chatKeys.sessions(classId) }),
    queryClient.invalidateQueries({ queryKey: agentKeys.workspace(classId) }),
    queryClient.invalidateQueries({ queryKey: agentKeys.activity(classId, sessionId) }),
    queryClient.invalidateQueries({ queryKey: agentKeys.changes(classId, sessionId) }),
    queryClient.invalidateQueries({ queryKey: agentKeys.commands(classId, sessionId) }),
    queryClient.invalidateQueries({ queryKey: profileKeys.forClass(classId) }),
    queryClient.invalidateQueries({ queryKey: draftKeys.sources(classId) }),
  ])
}

export function useAgentWorkspace(classId: number) {
  return useQuery({
    queryKey: agentKeys.workspace(classId),
    queryFn: ({ signal }) => api.getAgentWorkspace(classId, signal),
  })
}

export function useAttachAgentWorkspace(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ rootPath, displayName }: { rootPath: string; displayName?: string }) =>
      api.attachAgentWorkspace(classId, rootPath, displayName),
    onSuccess: (workspace) => queryClient.setQueryData(agentKeys.workspace(classId), workspace),
  })
}

export function useDetachAgentWorkspace(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.detachAgentWorkspace(classId),
    onSuccess: () => queryClient.setQueryData(agentKeys.workspace(classId), null),
  })
}

export function useUpdateAgentWorkspaceGrants(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: AgentWorkspaceGrantsUpdate) => api.updateAgentWorkspaceGrants(classId, body),
    onSuccess: (workspace) => queryClient.setQueryData(agentKeys.workspace(classId), workspace),
  })
}

export function useAgentActivity(classId: number, sessionId: number | null) {
  return useQuery({
    queryKey: agentKeys.activity(classId, sessionId ?? -1),
    queryFn: ({ signal }) => api.listAgentActivity(classId, sessionId as number, signal),
    enabled: sessionId !== null,
    refetchInterval: 2_000,
  })
}

export function useAgentChanges(classId: number, sessionId: number | null, enabled: boolean) {
  return useQuery({
    queryKey: agentKeys.changes(classId, sessionId ?? -1),
    queryFn: ({ signal }) => api.listAgentWorkspaceChanges(classId, sessionId as number, signal),
    enabled: sessionId !== null && enabled,
    refetchInterval: 2_000,
  })
}

export function useAgentCommands(classId: number, sessionId: number | null, enabled: boolean) {
  return useQuery({
    queryKey: agentKeys.commands(classId, sessionId ?? -1),
    queryFn: ({ signal }) => api.listAgentCommands(classId, sessionId as number, signal),
    enabled: sessionId !== null && enabled,
    refetchInterval: 2_000,
  })
}

export function useRefreshAgentSession(classId: number, sessionId: number | null) {
  const queryClient = useQueryClient()
  return () => {
    if (sessionId === null) return
    void queryClient.invalidateQueries({ queryKey: agentKeys.activity(classId, sessionId) })
    void queryClient.invalidateQueries({ queryKey: agentKeys.changes(classId, sessionId) })
    void queryClient.invalidateQueries({ queryKey: agentKeys.commands(classId, sessionId) })
  }
}

export function useSendAgentChat(classId: number, sessionId: number | null) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ content, profile }: { content: string; profile: AgentProfile }) => {
      if (sessionId === null) throw new Error('Start a conversation before using agent tools.')
      return api.sendAgentChat(classId, sessionId, content, profile)
    },
    onSuccess: async () => {
      if (sessionId === null) return
      await invalidateAgentTurnCaches(queryClient, classId, sessionId)
    },
    onError: async (error) => {
      if (sessionId === null || !(error instanceof AgentChatError)) return
      await invalidateAgentTurnCaches(queryClient, classId, sessionId)
    },
  })
}
