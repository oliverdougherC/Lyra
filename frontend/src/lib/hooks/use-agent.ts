'use client'

import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'

import { AgentChatError, api } from '@/lib/api'
import { chatKeys } from '@/lib/hooks/use-chat'
import { draftKeys } from '@/lib/hooks/use-drafts'
import { profileKeys } from '@/lib/hooks/use-profile'
import { OBSERVATION_ERROR_POLL_MS } from '@/lib/hooks/polling-policy'
import { parseTimestamp } from '@/lib/format'
import type { AgentAccessDismissalRead, AgentWorkspaceGrantsUpdate } from '@/types'
import type { AgentProfile } from '@/types'

export const agentKeys = {
  workspace: (classId: number) => ['agent', 'workspace', classId] as const,
  activity: (classId: number, sessionId: number) =>
    ['agent', 'activity', classId, sessionId] as const,
  changes: (classId: number, sessionId: number) =>
    ['agent', 'changes', classId, sessionId] as const,
  commands: (classId: number, sessionId: number) =>
    ['agent', 'commands', classId, sessionId] as const,
  dismissals: (classId: number, sessionId: number) =>
    ['agent', 'dismissals', classId, sessionId] as const,
  turns: (classId: number, sessionId: number | null) =>
    ['agent', 'turns', classId, sessionId ?? -1] as const,
}

/** The registry of live agent turns for a conversation (see `beginAgentTurnObservation`). */
type LiveTurnOwners = Record<string, true>

/** The cadence the work surface asks at while something agent-side is genuinely live. */
export const AGENT_POLL_MS = 2_000

export const AGENT_ERROR_POLL_MS = OBSERVATION_ERROR_POLL_MS

/**
 * A dismissal's bounded lifetime, mirrored from the backend's
 * `agent_store.ACCESS_DISMISSAL_TTL_SECONDS`: the server stops treating a dismissal as
 * active `dismissed_at + 30 min` after it was recorded, and nothing else ever changes the
 * list. The client never extends or shortens the window - it only uses the same clock
 * arithmetic to know when to look again.
 */
export const AGENT_DISMISSAL_TTL_MS = 30 * 60 * 1000

/**
 * How far past the earliest expiry the dismissal list re-checks (and the minimum gap
 * between re-checks once the window has already lapsed: the local and the backend's
 * clocks can differ by seconds, so a lapsed dismissal is confirmed by a short bounded
 * check, never by a rapid loop).
 */
export const DISMISSAL_POLL_MARGIN_MS = 5_000

/** The slice of a React Query query state the interval decisions read. */
export type AgentPollState = {
  data: unknown
  error: unknown
}

/**
 * The demand-driven cadence for the agent's observation queries.
 *
 * The old policy asked the same four questions on a timer no matter what, so a settled
 * conversation kept a 42-to-102-requests-per-minute heartbeat. Now a query asks again
 * only when there is a reason:
 *
 * - while `inFlight` (a live agent job or command the caller observed) the 2-second cadence
 *   keeps the surface truthful - this is the bounded observation of an active job, and it
 *   stops the moment nothing is live;
 * - while the last read failed, the bounded error cadence (`AGENT_ERROR_POLL_MS`) is the
 *   retry - slow enough to stop a rapid failing poll, fast enough that a backend that
 *   restarts is found within half a minute;
 * - otherwise the query is idle and asks nothing.
 *
 * Mutation and stream-completion invalidations (`invalidateAgentTurnCaches`) still do the
 * authoritative refreshes; this decides only what keeps asking between them.
 */
export function agentPollInterval(state: AgentPollState, inFlight: boolean): number | false {
  if (state.error) return AGENT_ERROR_POLL_MS
  return inFlight ? AGENT_POLL_MS : false
}

/**
 * The dismissal list's cadence: the server only ever removes dismissals by lapse
 * (recorded `dismissed_at`, active for `AGENT_DISMISSAL_TTL_MS` - the backend filters the
 * same way in `agent_store.get_active_dismissals`), so there is exactly one justified
 * moment to ask again - just after the earliest active dismissal expires. With no active
 * dismissal there is nothing to reconcile at all, and the query goes quiet: the card
 * that a lapsed dismissal was suppressing cannot re-surface before the server stops
 * serving the dismissal, and the expiry check is what clears the local copy.
 *
 * This replaces the unconditional 5-second full fetch while preserving the TTL behavior:
 * a card dismissed "Not now" stays dismissed across the window, and lapses back on the
 * same clock the server uses.
 */
export function dismissalPollInterval(
  state: {
    data: { dismissals: AgentAccessDismissalRead[] } | undefined
    error: unknown
  },
  now: number = Date.now(),
): number | false {
  if (state.error) return AGENT_ERROR_POLL_MS
  const dismissals = state.data?.dismissals ?? []
  if (dismissals.length === 0) return false
  let earliestExpiry = Number.POSITIVE_INFINITY
  for (const dismissal of dismissals) {
    const expiry = parseTimestamp(dismissal.dismissed_at).getTime() + AGENT_DISMISSAL_TTL_MS
    if (expiry < earliestExpiry) earliestExpiry = expiry
  }
  const delay = earliestExpiry - now
  return delay <= 0 ? DISMISSAL_POLL_MARGIN_MS : delay + DISMISSAL_POLL_MARGIN_MS
}

// ── Owner-scoped turn observation (PLA-509 cross-lane signal) ──────────────────────────────
//
// A turn started from the chat pane is invisible to the work surface's cache reads: the
// surface's settled cache can be empty, so inspecting `getQueryData` cannot discover a turn
// that has not written a row yet. The chat pane therefore announces its own turns here.
//
// Contract (the chat lane calls these from its turn lifecycle; this lane only defines and
// consumes them):
//
// - `beginAgentTurnObservation(client, classId, sessionId, owner)` marks `owner` live.
//   Call it once when the turn starts (the send opens, the stream begins). Idempotent per
//   owner.
// - `endAgentTurnObservation(client, classId, sessionId, owner)` releases `owner`. Call it
//   from that same turn's terminal/finally path (success or failure). It releases only the
//   named owner, so a late completion from an older turn cannot mark a newer turn idle, and
//   an unknown or stale owner is a no-op.
// - `owner` is any string unique to the turn (a fresh uuid, or the turn's message id).
//
// The registry is a per-conversation local query in the existing QueryClient cache - never a
// network read - so `useAgentTurnsLive` observes it reactively. It is in-memory only: a page
// reload clears it, which is correct because the reload also kills the stream the turn ran
// on.

export function beginAgentTurnObservation(
  queryClient: QueryClient,
  classId: number,
  sessionId: number,
  owner: string,
): void {
  queryClient.setQueryData<LiveTurnOwners>(agentKeys.turns(classId, sessionId), (current) => ({
    ...(current ?? {}),
    [owner]: true,
  }))
}

export function endAgentTurnObservation(
  queryClient: QueryClient,
  classId: number,
  sessionId: number,
  owner: string,
): void {
  queryClient.setQueryData<LiveTurnOwners>(agentKeys.turns(classId, sessionId), (current) => {
    if (!current || !(owner in current)) return current
    const next = { ...current }
    delete next[owner]
    return next
  })
}

/**
 * True while at least one announced turn for the conversation is live. This is the
 * work surface's "a turn is in flight" input: it opens the observation cadence for a
 * turn the pane started, from the moment it starts, even with an empty cache.
 */
export function useAgentTurnsLive(classId: number, sessionId: number | null): boolean {
  const queryClient = useQueryClient()
  const key = agentKeys.turns(classId, sessionId ?? -1)
  const data = useQuery({
    queryKey: key,
    // Subscribe to synchronous local state without an asynchronous seed fetch that
    // could replace an owner announced during mount. Unobserved entries use normal GC.
    queryFn: () => queryClient.getQueryData<LiveTurnOwners>(key) ?? {},
    initialData: () => queryClient.getQueryData<LiveTurnOwners>(key) ?? {},
    enabled: false,
    staleTime: Number.POSITIVE_INFINITY,
    refetchInterval: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  }).data
  return Object.keys(data ?? {}).length > 0
}

/**
 * Refresh everything one agent turn can have touched: the transcript, the class session list,
 * and the work-surface queries (workspace, activity, changes, commands, dismissals), plus the
 * cached surfaces that read from the same rows (profile, draft sources). Call it when an
 * agent turn settles outside the mutation hooks - the chat pane's inline agent turns settle
 * by hand, so a failed attempt row or a committed-but-lost reply would otherwise stay hidden
 * behind a stale cache.
 */
export async function invalidateAgentTurnCaches(
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
    queryClient.invalidateQueries({ queryKey: agentKeys.dismissals(classId, sessionId) }),
    queryClient.invalidateQueries({ queryKey: profileKeys.forClass(classId) }),
    queryClient.invalidateQueries({ queryKey: draftKeys.sources(classId) }),
  ])
}

export function useAgentWorkspace(classId: number) {
  return useQuery({
    queryKey: agentKeys.workspace(classId),
    refetchOnWindowFocus: 'always',
    queryFn: ({ signal }) => api.getAgentWorkspace(classId, signal),
  })
}

export function useAttachAgentWorkspace(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      rootPath,
      displayName,
      readEnabled,
    }: {
      rootPath: string
      displayName?: string
      readEnabled?: boolean
    }) => api.attachAgentWorkspace(classId, rootPath, { displayName, readEnabled }),
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

/**
 * The durable activity trail, observed on demand (PLA-509).
 *
 * `inFlight` is the work surface's observation of a live agent job (a tool call still
 * started, a proposal still pending, a command still pending or running, or a turn the
 * surface itself started and is awaiting). While it is live the 2-second cadence keeps
 * the trail truthful; once nothing is live the query goes quiet, and the turn's own
 * settlement invalidation (or a window-focus reconciliation) is what brings it back.
 */
export function useAgentActivity(classId: number, sessionId: number | null, inFlight: boolean) {
  return useQuery({
    queryKey: agentKeys.activity(classId, sessionId ?? -1),
    queryFn: ({ signal }) => api.listAgentActivity(classId, sessionId as number, signal),
    enabled: sessionId !== null,
    refetchInterval: (query) => agentPollInterval(query.state, inFlight),
    // The app turns off refetch-on-focus globally; this query opts back in on its own so
    // returning from a hidden window reconciles with one authoritative read instead of
    // sitting on whatever the paused poll last saw (the interval timer itself is paused
    // while hidden and does not re-arm its skipped tick).
    refetchOnWindowFocus: 'always',
  })
}

/**
 * Active access dismissals for the conversation, observed on demand (PLA-509).
 *
 * The list only ever changes two ways: the student records a dismissal (the mutation
 * below invalidates it immediately) or a dismissal lapses on the server's own clock
 * (`AGENT_DISMISSAL_TTL_MS` after `dismissed_at`). The cadence is that second moment,
 * computed from the data (`dismissalPollInterval`) - a bounded expiry check, not the
 * old unconditional 5-second full fetch.
 */
export function useAgentAccessDismissals(classId: number, sessionId: number | null) {
  return useQuery({
    queryKey: agentKeys.dismissals(classId, sessionId ?? -1),
    queryFn: ({ signal }) => api.listAgentAccessDismissals(classId, sessionId as number, signal),
    enabled: sessionId !== null,
    refetchInterval: (query) => dismissalPollInterval(query.state),
    refetchOnWindowFocus: 'always',
    select: (data) => new Set(data.dismissals.map((d) => d.scope)),
  })
}

export function useDismissAgentAccess(classId: number, sessionId: number | null) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (scope: string) => api.dismissAgentAccess(classId, sessionId as number, scope),
    onSuccess: () => {
      // The dismissal is server state with a bounded lifetime: refresh it so the card
      // stays dismissed across reloads, and the model's next ask sees it too.
      if (sessionId !== null) {
        void queryClient.invalidateQueries({
          queryKey: agentKeys.dismissals(classId, sessionId),
        })
      }
    },
  })
}

/**
 * The workspace's pending file proposals, observed on demand (PLA-509). A proposal only
 * moves while a turn is live or the proposal itself is live (pending / partially applied
 * - `stale` needs no asking: nothing rewrites it but a student re-check, which refreshes
 * on its own, or a new turn, which the shared in-flight signal covers), so a settled
 * workspace stops asking every two seconds.
 */
export function useAgentChanges(
  classId: number,
  sessionId: number | null,
  enabled: boolean,
  inFlight: boolean,
) {
  return useQuery({
    queryKey: agentKeys.changes(classId, sessionId ?? -1),
    queryFn: ({ signal }) => api.listAgentWorkspaceChanges(classId, sessionId as number, signal),
    enabled: sessionId !== null && enabled,
    refetchInterval: (query) => agentPollInterval(query.state, inFlight),
    refetchOnWindowFocus: 'always',
  })
}

/**
 * The workspace's command requests, observed on demand (PLA-509). The same demand rule as
 * the proposals, with the live set `pending` or `running`: a command that starts running
 * must be watched to its exit code, and a new proposal appears only while a turn is live.
 */
export function useAgentCommands(
  classId: number,
  sessionId: number | null,
  enabled: boolean,
  inFlight: boolean,
) {
  return useQuery({
    queryKey: agentKeys.commands(classId, sessionId ?? -1),
    queryFn: ({ signal }) => api.listAgentCommands(classId, sessionId as number, signal),
    enabled: sessionId !== null && enabled,
    refetchInterval: (query) => agentPollInterval(query.state, inFlight),
    refetchOnWindowFocus: 'always',
  })
}

export function useRefreshAgentSession(classId: number, sessionId: number | null) {
  const queryClient = useQueryClient()
  return () => {
    if (sessionId === null) return
    void queryClient.invalidateQueries({ queryKey: agentKeys.activity(classId, sessionId) })
    void queryClient.invalidateQueries({ queryKey: agentKeys.changes(classId, sessionId) })
    void queryClient.invalidateQueries({ queryKey: agentKeys.commands(classId, sessionId) })
    void queryClient.invalidateQueries({ queryKey: agentKeys.dismissals(classId, sessionId) })
  }
}

export function useSendAgentChat(classId: number, sessionId: number | null) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      content,
      profile,
      operationId,
    }: {
      content: string
      profile?: AgentProfile
      operationId?: string
    }) => {
      if (sessionId === null) throw new Error('Start a conversation before using agent tools.')
      return api.sendAgentChat(
        classId,
        sessionId,
        content,
        profile,
        undefined,
        undefined,
        operationId,
      )
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

/**
 * Retry the conversation's last failed agent turn (PLA-295).
 *
 * Reuses the original user message rather than sending a new one, so pressing Retry is
 * "answer the failed turn again", not "ask twice". Repeated clicks are serialized by the
 * server's per-session claim - a second one racing the first returns a 409 - so the button
 * is also disabled while a retry is pending; the two together keep at most one retry in
 * flight. Both endings refresh the transcript and activity, so the turn's truthful state
 * (a new reply, or a still-failed attempt) is what the conversation shows next.
 */
export function useRetryAgentChat(classId: number, sessionId: number | null) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => {
      if (sessionId === null) throw new Error('Start a conversation before using agent tools.')
      return api.retryAgentChat(classId, sessionId)
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

/**
 * Regenerate the conversation's last agent answer, superseding the reply it already has.
 * Re-runs the turn even when it completed, so a completed answer can be re-answered.
 */
export function useRegenerateAgentChat(classId: number, sessionId: number | null) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => {
      if (sessionId === null) throw new Error('Start a conversation before using agent tools.')
      return api.regenerateAgentChat(classId, sessionId)
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
