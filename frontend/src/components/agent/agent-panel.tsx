'use client'

import { useState } from 'react'
import { FolderLock, RefreshCw, X } from 'lucide-react'
import { toast } from 'sonner'

import { AgentActivityFeed } from '@/components/agent/activity-cards'
import { CapabilitySummary } from '@/components/agent/capability-summary'
import { CommandConfirmationCard } from '@/components/agent/command-confirmation'
import { WorkspaceChangeReviewRail } from '@/components/agent/workspace-change-review'
import { SourceLedger } from '@/components/drafts/source-ledger'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { ScrollArea } from '@/components/ui/scroll-area'
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
import { useClassWriterSettings, useUpdateClassWriterSettings } from '@/lib/hooks/use-settings'
import type { AgentProfile } from '@/types'
import { hunksAreStale } from './types'
import type { AgentGrantKey, AgentToolActivity } from './types'

type AgentPanelProps = {
  classId: number
  sessionId: number | null
  onClose?: () => void
}

export function AgentPanel({ classId, sessionId, onClose }: AgentPanelProps) {
  const [rootPath, setRootPath] = useState('')
  const [prompt, setPrompt] = useState('')
  const [profile, setProfile] = useState<AgentProfile>('research')
  const [effectBusy, setEffectBusy] = useState(false)
  const workspace = useAgentWorkspace(classId)
  const writerSettings = useClassWriterSettings(classId)
  const attach = useAttachAgentWorkspace(classId)
  const detach = useDetachAgentWorkspace(classId)
  const updateGrants = useUpdateAgentWorkspaceGrants(classId)
  const updateWriterSettings = useUpdateClassWriterSettings()
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
  const busy =
    attach.isPending || detach.isPending || updateGrants.isPending || updateWriterSettings.isPending

  const toggleGrant = (key: AgentGrantKey, enabled: boolean) => {
    if (key === 'web') {
      updateWriterSettings.mutate(
        { classId, body: { allow_web_research: enabled } },
        { onError: (error) => toast.error(error.message) },
      )
      return
    }
    const field =
      key === 'workspace_read'
        ? 'read_enabled'
        : key === 'change_proposals'
          ? 'change_proposals_enabled'
          : 'commands_enabled'
    updateGrants.mutate({ [field]: enabled }, { onError: (error) => toast.error(error.message) })
  }

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

  const grants = [
    {
      key: 'web' as const,
      label: 'Web',
      description: 'Search public sources and fetch public URLs through Exa.',
      enabled: writerSettings.data?.effective.allow_web_research ?? false,
      inherited: writerSettings.data?.overrides.allow_web_research === null,
    },
    {
      key: 'workspace_read' as const,
      label: 'Workspace read',
      description: 'List, search, and read bounded text files under the attached root.',
      enabled: workspace.data?.read_enabled ?? false,
      unavailable: !workspace.data,
    },
    {
      key: 'change_proposals' as const,
      label: 'Change proposals',
      description: 'Create inert file diffs that require your hunk-by-hunk approval.',
      enabled: workspace.data?.change_proposals_enabled ?? false,
      unavailable: !workspace.data,
      blockedReason:
        workspace.data && !workspace.data.read_enabled
          ? 'Workspace read must be enabled first.'
          : undefined,
    },
    {
      key: 'commands' as const,
      label: 'Commands',
      description: 'Propose exact verification argv; every run still needs confirmation.',
      enabled: workspace.data?.commands_enabled ?? false,
      unavailable: !workspace.data,
    },
  ]

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
    <aside className="flex h-full min-h-0 flex-col bg-background" aria-label="Agent controls">
      <header className="border-border flex h-14 shrink-0 items-center gap-2 border-b px-4">
        <FolderLock className="text-text-tertiary size-4" aria-hidden />
        <h2 className="flex-1 text-sm font-medium">Agent controls</h2>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Refresh agent activity"
          onClick={refresh}
        >
          <RefreshCw className="size-3.5" />
        </Button>
        {onClose ? (
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Close agent controls"
            onClick={onClose}
          >
            <X className="size-3.5" />
          </Button>
        ) : null}
      </header>
      <ScrollArea className="min-h-0 flex-1">
        <div className="flex flex-col gap-4 p-4">
          <form
            className="flex flex-col gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              const content = prompt.trim()
              if (!content) return
              sendAgentChat.mutate(
                { content, profile },
                {
                  onSuccess: () => setPrompt(''),
                  onError: (error) => toast.error(error.message),
                },
              )
            }}
          >
            <label htmlFor="agent-profile" className="text-sm font-medium">
              Agent turn
            </label>
            <Select value={profile} onValueChange={(value) => setProfile(value as AgentProfile)}>
              <SelectTrigger id="agent-profile" aria-label="Agent profile">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="research">Research · public web only</SelectItem>
                <SelectItem value="code">Code · workspace only</SelectItem>
                <SelectItem value="command">Command · proposal only</SelectItem>
              </SelectContent>
            </Select>
            <Textarea
              value={prompt}
              placeholder={
                profile === 'research'
                  ? 'Research a public topic…'
                  : profile === 'code'
                    ? 'Inspect or propose a repository change…'
                    : 'Propose one verification command…'
              }
              rows={4}
              maxLength={20_000}
              onChange={(event) => setPrompt(event.target.value)}
            />
            <Button
              type="submit"
              size="sm"
              disabled={sessionId === null || !prompt.trim() || sendAgentChat.isPending}
            >
              {sendAgentChat.isPending ? 'Working…' : 'Send to this conversation'}
            </Button>
            <p className="text-text-tertiary text-xs">
              Profiles are isolated. The agent can only propose host effects; you apply changes or
              run commands separately below.
            </p>
          </form>

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

          {!workspace.data ? (
            <form
              className="flex flex-col gap-2"
              onSubmit={(event) => {
                event.preventDefault()
                attach.mutate(
                  { rootPath },
                  {
                    onSuccess: () => setRootPath(''),
                    onError: (error) => toast.error(error.message),
                  },
                )
              }}
            >
              <label htmlFor="agent-root" className="text-sm font-medium">
                Attach local workspace
              </label>
              <Input
                id="agent-root"
                value={rootPath}
                placeholder="/absolute/path/to/repository"
                onChange={(event) => setRootPath(event.target.value)}
              />
              <Button type="submit" size="sm" disabled={!rootPath.trim() || attach.isPending}>
                Attach with all grants off
              </Button>
            </form>
          ) : null}

          <CapabilitySummary
            workspace={
              workspace.data
                ? { label: workspace.data.display_name, rootPath: workspace.data.root_path }
                : null
            }
            grants={grants}
            busy={busy}
            onToggleGrant={toggleGrant}
          />
          {workspace.data ? (
            <Button
              variant="outline"
              size="sm"
              disabled={detach.isPending}
              onClick={() =>
                detach.mutate(undefined, { onError: (error) => toast.error(error.message) })
              }
            >
              Detach workspace
            </Button>
          ) : null}

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

          <AgentActivityFeed entries={activityEntries} />
          <SourceLedger classId={classId} />
        </div>
      </ScrollArea>
    </aside>
  )
}
