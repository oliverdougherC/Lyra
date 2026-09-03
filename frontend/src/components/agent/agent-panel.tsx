'use client'

import { useMemo, useState } from 'react'
import { Folder, MoreHorizontal, RefreshCw, X } from 'lucide-react'
import { toast } from 'sonner'

import { AgentActivityFeed } from '@/components/agent/activity-cards'
import { CommandConfirmationCard } from '@/components/agent/command-confirmation'
import { WorkspaceChangeReviewRail } from '@/components/agent/workspace-change-review'
import { SourceLedger } from '@/components/drafts/source-ledger'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Spinner } from '@/components/ui/spinner'
import { Textarea } from '@/components/ui/textarea'
import { api } from '@/lib/api'
import {
  useAgentActivity,
  useAgentChanges,
  useAgentCommands,
  useAgentWorkspace,
  useAttachAgentWorkspace,
  useDetachAgentWorkspace,
  useRefreshAgentSession,
  useRetryAgentChat,
  useSendAgentChat,
  useUpdateAgentWorkspaceGrants,
} from '@/lib/hooks/use-agent'
import { useMessages } from '@/lib/hooks/use-chat'
import { desktopFolderPickerAvailable, pickDesktopWorkspaceDirectory } from '@/lib/runtime'
import type { AgentAuditEventRead, AgentWorkspaceGrantsUpdate } from '@/types'
import { hunksAreStale } from './types'
import type { AgentToolActivity } from './types'

type AgentPanelProps = {
  classId: number
  sessionId: number | null
  onClose?: () => void
}

// The just-in-time access the contextual agent can ask for (backend `request_workspace_access`).
const ACCESS_SCOPE_LABELS: Record<string, { title: string; detail: string }> = {
  attach: {
    title: 'Attach a local folder',
    detail: 'Choose a folder; Lyra can read the files in it.',
  },
  read: {
    title: 'Read the attached folder',
    detail: 'Lyra can list and read text files under it.',
  },
  propose_changes: {
    title: 'Prepare file edits',
    detail: 'Lyra can draft changes you review hunk by hunk before anything is applied.',
  },
  run_commands: {
    title: 'Prepare verification commands',
    detail: 'Lyra can propose exact commands; every run still needs your approval.',
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

export function AgentPanel({ classId, sessionId, onClose }: AgentPanelProps) {
  const [prompt, setPrompt] = useState('')
  const [attachPath, setAttachPath] = useState('')
  const [attachPathVisible, setAttachPathVisible] = useState(false)
  const [dismissedScopes, setDismissedScopes] = useState<ReadonlySet<string>>(new Set())
  const [detailsOpen, setDetailsOpen] = useState(false)
  const [effectBusy, setEffectBusy] = useState(false)
  const workspace = useAgentWorkspace(classId)
  const attach = useAttachAgentWorkspace(classId)
  const detach = useDetachAgentWorkspace(classId)
  const updateGrants = useUpdateAgentWorkspaceGrants(classId)
  const activity = useAgentActivity(classId, sessionId)
  const changes = useAgentChanges(classId, sessionId, Boolean(workspace.data))
  const commands = useAgentCommands(classId, sessionId, Boolean(workspace.data))
  const refresh = useRefreshAgentSession(classId, sessionId)
  const sendAgentChat = useSendAgentChat(classId, sessionId)
  const retryAgentChat = useRetryAgentChat(classId, sessionId)
  const messages = useMessages(sessionId)
  // The conversation's last turn, when it was an agent turn that failed or stopped. This is
  // the turn Retry re-answers: it reuses that message rather than sending a new one, so a
  // failed turn is retried, never duplicated (PLA-295).
  const lastMessage = messages.data?.[messages.data.length - 1]
  const failedTurn =
    lastMessage?.role === 'user' &&
    (lastMessage.agent_attempt?.state === 'failed' ||
      lastMessage.agent_attempt?.state === 'stopped')
      ? lastMessage
      : null
  const busy = attach.isPending || detach.isPending || updateGrants.isPending

  // Attach through the normal path for the current platform: the native folder picker on
  // desktop, a bounded path entry when no picker exists (browser build).
  const attachFolder = (rootPath: string) => {
    attach.mutate(
      // A just-in-time attach starts with reading - the minimum for inspecting a project.
      // Deeper grants (edits, commands) are requested separately when a task needs them.
      { rootPath, readEnabled: true },
      {
        onSuccess: () => {
          setAttachPath('')
          setAttachPathVisible(false)
          refresh()
        },
        onError: (error) => toast.error(error.message),
      },
    )
  }

  const openFolderPicker = async () => {
    const path = await pickDesktopWorkspaceDirectory()
    if (path) {
      attachFolder(path)
    } else if (!desktopFolderPickerAvailable()) {
      setAttachPathVisible(true)
    }
    // A cancelled native picker leaves the card as-is: choosing is still possible.
  }

  const approveAccess = (scope: string) => {
    if (scope === 'attach') {
      void openFolderPicker()
      return
    }
    const body: AgentWorkspaceGrantsUpdate = {}
    if (scope === 'read') {
      body.read_enabled = true
    }
    if (scope === 'propose_changes') {
      // Editing presupposes reading: when the read grant is still off, approve the pair.
      body.change_proposals_enabled = true
      if (workspace.data && !workspace.data.read_enabled) body.read_enabled = true
    }
    if (scope === 'run_commands') {
      body.commands_enabled = true
    }
    updateGrants.mutate(body, {
      onSuccess: () => refresh(),
      onError: (error) => toast.error(error.message),
    })
  }

  const dismissAccess = (scope: string) => {
    setDismissedScopes(new Set([...dismissedScopes, scope]))
  }

  // One card per scope the last agent run asked for and that is still missing. Once the
  // student approves (or detaches/re-attaches) the grant state moves and the card leaves -
  // it is derived, so there is nothing to remember and no dashboard to manage.
  const pendingRequests = useMemo(() => {
    const latest = new Map<string, AgentAuditEventRead>()
    for (const event of activity.data ?? []) {
      if (event.target_kind !== 'capability_request' || event.state !== 'succeeded') continue
      const scope = event.target_id
      if (scope && ACCESS_SCOPE_LABELS[scope]) latest.set(scope, event)
    }
    return [...latest.entries()]
      .map(([scope]) => scope)
      .filter(
        (scope) => !dismissedScopes.has(scope) && !accessScopeSatisfied(workspace.data, scope),
      )
  }, [activity.data, dismissedScopes, workspace.data])

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

  return (
    <aside className="flex h-full min-h-0 flex-col bg-background" aria-label="Agent">
      <header className="border-border flex h-14 shrink-0 items-center gap-2 border-b px-4">
        <Folder className="text-text-tertiary size-4" aria-hidden />
        <h2 className="flex-1 text-sm font-medium">Agent</h2>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Refresh agent activity"
          onClick={refresh}
        >
          <RefreshCw className="size-3.5" />
        </Button>
        {onClose ? (
          <Button variant="ghost" size="icon-sm" aria-label="Close agent panel" onClick={onClose}>
            <X className="size-3.5" />
          </Button>
        ) : null}
      </header>
      <ScrollArea className="min-h-0 flex-1">
        <div className="flex flex-col gap-4 p-4">
          {workspace.data ? (
            <div className="flex items-center gap-1.5">
              <span className="text-text-secondary inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs font-medium">
                <Folder className="size-3" aria-hidden />
                Workspace: {workspace.data.display_name}
              </span>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="ghost" size="icon-sm" aria-label="Workspace options">
                    <MoreHorizontal className="size-3.5" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start">
                  <DropdownMenuItem
                    onSelect={() =>
                      detach.mutate(undefined, {
                        onError: (error) => toast.error(error.message),
                      })
                    }
                  >
                    Detach workspace
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          ) : attachPathVisible ? (
            <form
              className="flex flex-col gap-2"
              onSubmit={(event) => {
                event.preventDefault()
                if (!attachPath.trim()) return
                attachFolder(attachPath.trim())
              }}
            >
              <label htmlFor="agent-attach-path" className="text-sm font-medium">
                Attach a local folder
              </label>
              <Input
                id="agent-attach-path"
                value={attachPath}
                placeholder="/absolute/path/to/repository"
                onChange={(event) => setAttachPath(event.target.value)}
              />
              <div className="flex items-center gap-2">
                <Button type="submit" size="sm" disabled={!attachPath.trim() || attach.isPending}>
                  Attach
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  onClick={() => setAttachPathVisible(false)}
                >
                  Cancel
                </Button>
              </div>
            </form>
          ) : (
            <Button
              variant="outline"
              size="sm"
              disabled={attach.isPending}
              onClick={() => void openFolderPicker()}
            >
              <Folder aria-hidden />
              {attach.isPending ? 'Attaching…' : 'Choose a folder to work in'}
            </Button>
          )}

          <form
            className="flex flex-col gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              const content = prompt.trim()
              if (!content) return
              // No profile in the payload: the contextual agent plans the work itself.
              sendAgentChat.mutate(
                { content },
                {
                  onSuccess: () => setPrompt(''),
                  onError: (error) => toast.error(error.message),
                },
              )
            }}
          >
            <label htmlFor="agent-prompt" className="text-sm font-medium">
              Ask Lyra
            </label>
            <Textarea
              id="agent-prompt"
              value={prompt}
              placeholder="Read the starter project and explain how the pieces fit together…"
              rows={4}
              maxLength={20_000}
              onChange={(event) => setPrompt(event.target.value)}
            />
            <div className="flex items-end justify-between gap-3">
              <p className="text-text-tertiary flex-1 pb-1 text-xs">
                Lyra plans the tools a task needs. Edits and commands ask before they happen.
              </p>
              <Button
                type="submit"
                size="sm"
                disabled={sessionId === null || !prompt.trim() || sendAgentChat.isPending}
              >
                Send
              </Button>
            </div>
          </form>

          {sendAgentChat.isPending ? (
            <div className="border-border flex items-center gap-2 rounded-md border px-3 py-2">
              <Spinner className="text-text-tertiary size-3.5" />
              <span className="text-sm">Working in this conversation…</span>
            </div>
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

          {pendingRequests.map((scope) => (
            <div
              key={scope}
              data-access-request={scope}
              className="border-border flex flex-col gap-2 rounded-md border p-3"
            >
              <p className="text-sm font-medium">{ACCESS_SCOPE_LABELS[scope].title}</p>
              <p className="text-text-secondary text-xs">{ACCESS_SCOPE_LABELS[scope].detail}</p>
              <div className="flex items-center gap-2">
                <Button size="sm" disabled={busy} onClick={() => approveAccess(scope)}>
                  {scope === 'attach' && !desktopFolderPickerAvailable()
                    ? 'Attach a folder'
                    : 'Approve'}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => dismissAccess(scope)}>
                  Not now
                </Button>
              </div>
            </div>
          ))}

          {sessionId === null ? (
            <Alert>
              <AlertTitle>Start a conversation first</AlertTitle>
              <AlertDescription>
                Proposals and durable activity are scoped to the conversation that created them.
              </AlertDescription>
            </Alert>
          ) : null}

          {(changes.data ?? []).map((change) => (
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

          {(commands.data ?? []).map((command) => (
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
        </div>
      </ScrollArea>
    </aside>
  )
}
