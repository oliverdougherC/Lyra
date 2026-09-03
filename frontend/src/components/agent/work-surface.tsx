'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'

import { AgentActivityFeed } from '@/components/agent/activity-cards'
import { CommandConfirmationCard } from '@/components/agent/command-confirmation'
import { AttachPathEntry, useWorkspaceAttach } from '@/components/agent/workspace-attach'
import { WorkspaceChangeReviewRail } from '@/components/agent/workspace-change-review'
import { SourceLedger } from '@/components/drafts/source-ledger'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import {
  useAgentAccessDismissals,
  useAgentActivity,
  useAgentChanges,
  useAgentCommands,
  useDismissAgentAccess,
  useRefreshAgentSession,
  useRegenerateAgentChat,
  useRetryAgentChat,
} from '@/lib/hooks/use-agent'
import { useMessages } from '@/lib/hooks/use-chat'
import { ApiError, api } from '@/lib/api'
import { parseTimestamp } from '@/lib/format'
import type { AgentAuditEventRead } from '@/types'
import { hunksAreStale } from './types'
import type { AgentToolActivity } from './types'

type AgentWorkSurfaceProps = {
  classId: number
  sessionId: number | null
}

// The just-in-time access the contextual agent can ask for (backend `request_workspace_access`).
// Each card carries three things: what the student is being asked (title), why *this*
// task needs it now (the model's `reason`, rendered as the card's main line), and what
// granting enables versus what still needs its own review.
const ACCESS_SCOPE_LABELS: Record<string, { title: string; enables: string; review: string }> = {
  attach: {
    title: 'Attach a local folder',
    enables: 'Lyra can look at the folder and ask for specific access when a task needs it.',
    review: 'Reading, file edits, and command runs each still need their own approval.',
  },
  read: {
    title: 'Read the attached folder',
    enables: 'Lyra can list and read the text files in the folder.',
    review: 'File edits and command runs each still need their own approval.',
  },
  propose_changes: {
    title: 'Prepare file edits',
    enables:
      'Lyra can draft changes for you to review hunk by hunk; nothing is applied until you accept it.',
    review: 'Command runs each still need their own approval.',
  },
  run_commands: {
    title: 'Prepare verification commands',
    enables: 'Lyra can propose the exact command, its working folder, and what it checks.',
    review: 'Each run needs your explicit approval before it happens.',
  },
}

function accessScopeSatisfied(
  workspace:
    | {
        read_enabled: boolean
        change_proposals_enabled: boolean
        commands_enabled: boolean
      }
    | null
    | undefined,
  scope: string,
): boolean {
  if (scope === 'attach') return workspace !== null
  if (!workspace) return false
  if (scope === 'read') return workspace.read_enabled
  if (scope === 'propose_changes') return workspace.change_proposals_enabled
  if (scope === 'run_commands') return workspace.commands_enabled
  return true
}

/**
 * The contextual agent's work surface, rendered as part of the ordinary class
 * conversation rather than a separate cockpit. It owns no composer - the conversation's
 * own composer sends the turn - and it shows only what is live: a just-in-time access
 * request (with the model's task-specific reason), pending edits to review, pending
 * commands to approve, the failed-turn retry, and the durable activity trail. The
 * attached workspace is the composer's context chip (`WorkspaceContextChip`), so with
 * no live work this surface renders nothing at all.
 *
 * When a turn asks for access and ends on the request, and the student then resolves
 * that request (attaching the folder, approving a scope), the work the turn was for has
 * not happened yet. The surface continues the turn for them: it regenerates the last
 * question, so the re-answered reply inspects the newly granted access in the same
 * transcript - one conversation, no re-asking, no second composer.
 */
export function AgentWorkSurface({ classId, sessionId }: AgentWorkSurfaceProps) {
  const [detailsOpen, setDetailsOpen] = useState(false)
  const [effectBusy, setEffectBusy] = useState(false)
  const {
    workspace: workspaceData,
    attachPending,
    attachFolder,
    approveAccess,
    lastResolution,
    cardPathEntryVisible,
    setCardPathEntryVisible,
  } = useWorkspaceAttach()
  const activity = useAgentActivity(classId, sessionId)
  const changes = useAgentChanges(classId, sessionId, Boolean(workspaceData))
  const commands = useAgentCommands(classId, sessionId, Boolean(workspaceData))
  const dismissals = useAgentAccessDismissals(classId, sessionId)
  const dismiss = useDismissAgentAccess(classId, sessionId)
  const refresh = useRefreshAgentSession(classId, sessionId)
  const retryAgentChat = useRetryAgentChat(classId, sessionId)
  const regenerateAgentChat = useRegenerateAgentChat(classId, sessionId)
  const messages = useMessages(sessionId)

  // The conversation's own composer drives the turn, so this surface learns that a turn
  // committed by watching the transcript grow: a new assistant reply is what triggers a
  // re-fetch of the durable artifacts (activity, proposals, commands, dismissals).
  const messageCount = messages.data?.length ?? 0
  const lastCountRef = useRef<number | null>(null)
  const refreshRef = useRef(refresh)
  refreshRef.current = refresh
  useEffect(() => {
    if (lastCountRef.current !== null && lastCountRef.current !== messageCount) refreshRef.current()
    lastCountRef.current = messageCount
  }, [messageCount])

  // The conversation's last turn, when it was an agent turn that failed or stopped. This is
  // the turn Retry re-answers: it reuses that message rather than sending a new one (PLA-295).
  const lastMessage = messages.data?.[messages.data.length - 1]
  const failedTurn =
    lastMessage?.role === 'user' &&
    (lastMessage.agent_attempt?.state === 'failed' ||
      lastMessage.agent_attempt?.state === 'stopped')
      ? lastMessage
      : null

  const dismissAccess = (scope: string) => {
    // "Not now" is server state with a bounded lifetime: recorded against this
    // conversation (agent_store.ACCESS_DISMISSAL_TTL_SECONDS), so it survives reloads
    // and unmounts, keeps the model from asking again, and lapses instead of persisting.
    dismiss.mutate(scope, {
      onError: (error) =>
        toast.error(error instanceof Error ? error.message : 'Could not save that.'),
    })
  }

  // One card per scope the agent asked for that is still missing and not deferred. The
  // card's main line is the model's task-specific reason (durably stored with the
  // request), not a status phrase. Deferred scopes are suppressed by the server's
  // bounded dismissal, not by local state, so unmounting or reloading does not resurface
  // them.
  const pendingRequests = useMemo(() => {
    const latest = new Map<string, AgentAuditEventRead>()
    for (const event of activity.data ?? []) {
      if (event.target_kind !== 'capability_request' || event.state !== 'succeeded') continue
      const scope = event.target_id
      if (scope && ACCESS_SCOPE_LABELS[scope]) latest.set(scope, event)
    }
    const dismissed = dismissals.data ?? new Set<string>()
    return [...latest.entries()]
      .filter(([scope]) => !dismissed.has(scope) && !accessScopeSatisfied(workspaceData, scope))
      .map(([scope, event]) => {
        const summary = event.result_summary
        const reason = summary && typeof summary.reason === 'string' ? summary.reason : null
        return { scope, reason }
      })
  }, [activity.data, dismissals.data, workspaceData])

  // ── Just-in-time access continuation ──────────────────────────────────────────────
  // The turn a student asked for asked for access and ended on the request. When the
  // student then resolves it (attaching the folder, approving a scope), the surface
  // continues that turn: the last question is regenerated and re-answered with the new
  // access, in the same transcript. Firing is bounded - the resolved scope must have
  // been asked by the conversation's LAST turn (approving a stale card after the student
  // moved on must not re-answer a newer question), every open request must be satisfied,
  // and each resolution fires at most once.
  const continuedKeyRef = useRef<string | null>(null)
  useEffect(() => {
    if (!lastResolution) return
    if (Date.now() - lastResolution.at > 60_000) return
    const list = messages.data ?? []
    // The turn must be settled: its reply is the last thing in the transcript.
    if (list[list.length - 1]?.role !== 'assistant') return
    const lastUser = [...list].reverse().find((message) => message.role === 'user' && message.agent_attempt)
    if (!lastUser) return
    const turnStart = parseTimestamp(lastUser.created_at).getTime()
    const askedInLastTurn = (activity.data ?? []).some(
      (event) =>
        event.target_kind === 'capability_request' &&
        event.state === 'succeeded' &&
        event.target_id !== null &&
        lastResolution.scopes.includes(event.target_id) &&
        Date.parse(event.started_at) >= turnStart - 5_000,
    )
    if (!askedInLastTurn) return
    if (pendingRequests.length > 0) return
    const key = `${lastUser.id}:${lastResolution.at}`
    if (continuedKeyRef.current === key) return
    continuedKeyRef.current = key
    regenerateAgentChat.mutate(undefined, {
      onError: (error) => {
        // A 409 means another turn took the session claim first - the student is driving
        // (they sent their own message), so this continuation yields without a toast.
        if (!(error instanceof ApiError) || error.status !== 409) {
          toast.error(error instanceof Error ? error.message : 'Could not continue that turn.')
        }
      },
    })
  }, [activity.data, lastResolution, messages.data, pendingRequests, regenerateAgentChat])

  const acceptHunks = async (changeId: number, hunks: { index: number; hash: string }[]) => {
    if (sessionId === null) return
    setEffectBusy(true)
    try {
      const reviewed = await api.reviewAgentWorkspaceChange(classId, sessionId, changeId)
      const displayedCount =
        changes.data?.find((c) => c.id === changeId)?.hunks.length ?? hunks.length

      if (hunksAreStale(hunks, reviewed.hunks, displayedCount)) {
        refresh()
        toast.error('The proposal changed since you reviewed it. Please review the updated diff.')
        return
      }

      const confirmation = await api.confirmAgentWorkspaceChange(
        classId,
        sessionId,
        changeId,
        hunks,
      )
      await api.applyAgentWorkspaceChange(classId, sessionId, changeId, hunks, confirmation.token)
      refresh()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not apply that change.')
    } finally {
      setEffectBusy(false)
    }
  }

  const rejectChange = async (changeId: number) => {
    if (sessionId === null) return
    setEffectBusy(true)
    try {
      await api.rejectAgentWorkspaceChange(classId, sessionId, changeId)
      refresh()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not reject that change.')
    } finally {
      setEffectBusy(false)
    }
  }

  const runCommand = async (requestId: number) => {
    if (sessionId === null) return
    setEffectBusy(true)
    try {
      const confirmation = await api.confirmAgentCommand(classId, sessionId, requestId)
      await api.executeAgentCommand(classId, sessionId, requestId, confirmation.token)
      refresh()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not run that command.')
    } finally {
      setEffectBusy(false)
    }
  }

  const rejectCommand = async (requestId: number) => {
    if (sessionId === null) return
    setEffectBusy(true)
    try {
      await api.rejectAgentCommand(classId, sessionId, requestId)
      refresh()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not reject that command.')
    } finally {
      setEffectBusy(false)
    }
  }

  const activityEntries: AgentToolActivity[] = (activity.data ?? []).map((event) => ({
    id: event.id,
    title: event.tool.replaceAll('_', ' '),
    toolLabel: `${event.capability} · ${event.effect}`,
    targetLabel: event.target_id ?? undefined,
    timestampLabel: event.started_at,
    status:
      event.state === 'started'
        ? 'running'
        : event.state === 'succeeded'
          ? 'completed'
          : event.state === 'refused'
            ? 'disabled'
            : 'failed',
    failureReason: event.error_message ?? undefined,
    disabledReason: event.error_message ?? undefined,
  }))

  const pendingChanges = changes.data ?? []
  const pendingCommands = commands.data ?? []
  const hasActivity = (activity.data ?? []).length > 0
  const hasWork =
    pendingRequests.length > 0 ||
    pendingChanges.length > 0 ||
    pendingCommands.length > 0 ||
    failedTurn !== null ||
    hasActivity ||
    // The bounded path entry is live state the student is in the middle of: the surface
    // exists exactly as long as the form is on screen.
    cardPathEntryVisible

  // The surface is purely contextual: nothing live, nothing rendered. The attached
  // workspace lives in the composer's context chip, so an idle state has no surface of
  // its own - no docked strip, no setup block, no second composer.
  if (!hasWork) return null

  return (
    <section
      className="border-border bg-background flex min-h-0 flex-col gap-3 border-b px-4 py-3"
      aria-label="Agent work"
    >
      {cardPathEntryVisible && !workspaceData ? (
        // No native picker on this build (the browser): the bounded path entry stands in
        // for it, shown in the conversation surface next to the card that asked for the
        // folder, not as permanent setup chrome.
        <AttachPathEntry
          busy={attachPending}
          onSubmit={(rootPath) => attachFolder(rootPath)}
          onCancel={() => setCardPathEntryVisible(false)}
        />
      ) : null}

      {failedTurn ? (
        <Alert data-agent-retry variant="destructive">
          <AlertTitle>The last agent turn did not finish</AlertTitle>
          <AlertDescription className="flex flex-col items-start gap-2">
            <span>
              {failedTurn.agent_attempt?.detail?.trim() ||
                'The turn stopped before it produced a reply.'}
            </span>
            <Button
              size="sm"
              variant="outline"
              disabled={retryAgentChat.isPending}
              onClick={() =>
                retryAgentChat.mutate(undefined, {
                  onError: (error) => toast.error(error.message),
                })
              }
            >
              {retryAgentChat.isPending ? 'Retrying…' : 'Retry this turn'}
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}

      {pendingRequests.map(({ scope, reason }) => (
        <div
          key={scope}
          data-access-request={scope}
          className="border-border flex flex-col gap-2 rounded-md border p-3"
        >
          <p className="text-sm font-medium">{ACCESS_SCOPE_LABELS[scope].title}</p>
          {reason ? <p className="text-sm">{reason}</p> : null}
          <p className="text-text-secondary text-xs">
            {ACCESS_SCOPE_LABELS[scope].enables} {ACCESS_SCOPE_LABELS[scope].review}
          </p>
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              disabled={attachPending || dismiss.isPending}
              onClick={() => approveAccess(scope)}
            >
              {scope === 'attach' ? 'Attach a folder' : 'Approve'}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={dismiss.isPending}
              onClick={() => dismissAccess(scope)}
            >
              Not now
            </Button>
          </div>
        </div>
      ))}

      {pendingChanges.map((change) => (
        <WorkspaceChangeReviewRail
          key={change.id}
          busy={effectBusy}
          change={{
            id: change.id,
            path: change.path,
            rationale: change.rationale ?? undefined,
            state: change.state,
            currentContent: change.current_content ?? undefined,
            proposedContent: change.proposed_content ?? undefined,
            hunks: change.hunks,
          }}
          onAcceptHunk={(hunk) => void acceptHunks(change.id, [hunk])}
          onAcceptAll={() => void acceptHunks(change.id, change.hunks)}
          onRejectAll={() => void rejectChange(change.id)}
        />
      ))}

      {pendingCommands.map((command) => (
        <CommandConfirmationCard
          key={command.id}
          busy={effectBusy}
          command={{
            id: command.id,
            argv: command.argv,
            cwd: command.relative_cwd,
            reason: command.reason,
            expectedSignal: command.expected_signal ?? undefined,
            timeoutSeconds: command.timeout_seconds,
            networkRisk: 'unknown',
            state: command.state,
            stdout: command.stdout_text ?? undefined,
            stderr: command.stderr_text ?? undefined,
            exitCode: command.exit_code,
            truncated: command.truncated,
            confirmedAtLabel: command.confirmed_at ?? undefined,
          }}
          onConfirm={() => void runCommand(command.id)}
          onReject={() => void rejectCommand(command.id)}
        />
      ))}

      {hasActivity ? (
        <Collapsible open={detailsOpen} onOpenChange={setDetailsOpen}>
          <CollapsibleTrigger asChild>
            <button
              type="button"
              className="border-border hover:bg-accent flex w-full items-center justify-between rounded-md border px-3 py-2 text-sm font-medium"
              aria-expanded={detailsOpen}
            >
              <span>Details</span>
              <span className="text-text-tertiary text-xs">
                {(activity.data ?? []).length} activity events
              </span>
            </button>
          </CollapsibleTrigger>
          <CollapsibleContent className="flex flex-col gap-4 pt-3">
            <AgentActivityFeed entries={activityEntries} />
            <SourceLedger classId={classId} />
          </CollapsibleContent>
        </Collapsible>
      ) : null}
    </section>
  )
}
