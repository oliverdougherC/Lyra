'use client'

import { createContext, useContext, useMemo, useState, type ReactNode } from 'react'
import { Folder, MoreHorizontal } from 'lucide-react'
import { toast } from 'sonner'

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Spinner } from '@/components/ui/spinner'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import {
  useAttachAgentWorkspace,
  useAgentWorkspace,
  useDetachAgentWorkspace,
  useUpdateAgentWorkspaceGrants,
} from '@/lib/hooks/use-agent'
import { desktopFolderPickerAvailable, pickDesktopWorkspaceDirectory } from '@/lib/runtime'
import type { AgentWorkspaceGrantsUpdate, AgentWorkspaceRead } from '@/types'

/**
 * One workspace, owned once, visible in two places that both belong to the ordinary
 * conversation: the composer's context chip ("what Lyra has on hand") and the
 * just-in-time attach card in the work surface ("the task needs this folder"). Both go
 * through this provider, so the folder picker, the bounded path entry for builds without
 * one, and the grant approvals are the same code wherever the student reaches them.
 */

type WorkspaceAttachContextValue = {
  /** The attached workspace, `null` when the class has none. */
  workspace: AgentWorkspaceRead | null
  attachPending: boolean
  /** Attach a folder the student chose, starting with read - the minimum for inspection. */
  attachFolder: (rootPath: string) => void
  /**
   * The entry point behind "Attach a folder": the native picker where the desktop shell
   * offers one, the bounded path entry (shown in the work surface) where it does not.
   */
  beginAttach: () => Promise<void>
  /** Approve a just-in-time scope: the attach scope opens the picker; the rest flip grants. */
  approveAccess: (scope: string) => void
  /**
   * The most recent resolution the student made of a just-in-time access request:
   * which scopes became satisfied and when. The work surface uses it to continue a
   * turn that ended waiting on exactly this access.
   */
  lastResolution: { scopes: string[]; at: number } | null
  /** Where a build without a picker shows the path entry, and whether it is showing it. */
  cardPathEntryVisible: boolean
  setCardPathEntryVisible: (visible: boolean) => void
  detach: () => void
}
const WorkspaceAttachContext = createContext<WorkspaceAttachContextValue | null>(null)

export function WorkspaceAttachProvider({
  classId,
  children,
}: {
  classId: number
  children: ReactNode
}) {
  const workspace = useAgentWorkspace(classId)
  const attach = useAttachAgentWorkspace(classId)
  const detach = useDetachAgentWorkspace(classId)
  const updateGrants = useUpdateAgentWorkspaceGrants(classId)
  const [cardPathEntryVisible, setCardPathEntryVisible] = useState(false)
  const [lastResolution, setLastResolution] = useState<{ scopes: string[]; at: number } | null>(
    null,
  )

  const value = useMemo<WorkspaceAttachContextValue>(() => {
    const attachFolder = (rootPath: string) => {
      attach.mutate(
        // A just-in-time attach starts with reading - the minimum for inspecting a
        // project. Deeper grants (edits, commands) are requested separately when a task
        // needs them.
        { rootPath, readEnabled: true },
        {
          onSuccess: () => {
            setCardPathEntryVisible(false)
            // Attaching with read enabled satisfies the scopes a turn may have asked for:
            // the attach itself, and reading the attached folder.
            setLastResolution({ scopes: ['attach', 'read'], at: Date.now() })
          },
          onError: (error) => toast.error(error.message),
        },
      )
    }
    const beginAttach = async () => {
      const path = await pickDesktopWorkspaceDirectory()
      if (path) {
        attachFolder(path)
      } else if (!desktopFolderPickerAvailable()) {
        // No picker on this build: the bounded path entry stands in, and it belongs in
        // the conversation surface, next to the card that asked for it.
        setCardPathEntryVisible(true)
      }
      // A cancelled native picker leaves everything as-is: choosing is still possible.
    }
    const approveAccess = (scope: string) => {
      if (scope === 'attach') {
        void beginAttach()
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
        onSuccess: () => {
          // The scopes this approval satisfies: what was asked for, plus the read
          // grant that editing presupposes when it was still off.
          const scopes: string[] = []
          if (scope === 'read') scopes.push('read')
          if (scope === 'propose_changes') {
            scopes.push('propose_changes')
            if (body.read_enabled) scopes.push('read')
          }
          if (scope === 'run_commands') scopes.push('run_commands')
          setLastResolution({ scopes, at: Date.now() })
        },
        onError: (error) => toast.error(error.message),
      })
    }
    return {
      workspace: workspace.data ?? null,
      attachPending: attach.isPending || updateGrants.isPending || detach.isPending,
      attachFolder,
      beginAttach,
      approveAccess,
      lastResolution,
      cardPathEntryVisible,
      setCardPathEntryVisible,
      detach: () =>
        detach.mutate(undefined, {
          onError: (error) => toast.error(error.message),
        }),
    }
  }, [attach, detach, lastResolution, setLastResolution, updateGrants, workspace.data])

  return <WorkspaceAttachContext.Provider value={value}>{children}</WorkspaceAttachContext.Provider>
}

export function useWorkspaceAttach(): WorkspaceAttachContextValue {
  const context = useContext(WorkspaceAttachContext)
  if (!context) {
    throw new Error('useWorkspaceAttach must be used inside a WorkspaceAttachProvider.')
  }
  return context
}

/**
 * The attached workspace at the composer - the second of two small context marks on the
 * quiet line beneath the input, so "what Lyra has on hand for this task" reads in one
 * glance: the material the answer reads, and the local folder the work happens in. No
 * dashboard, no setup section: with no folder attached the affordance is a 24px icon (a
 * tooltip and an accessible name say what it does); once a folder is attached it becomes
 * a compact chip whose menu item detaches. Either way the input line is never given over
 * to setup.
 */
export function WorkspaceContextChip() {
  const { workspace, attachPending, beginAttach, detach } = useWorkspaceAttach()

  if (workspace) {
    return (
      <div className="flex items-center gap-1" data-workspace-chip>
        <span
          aria-label={`Working in the attached folder ${workspace.display_name}`}
          className="text-text-secondary inline-flex h-6 max-w-[7.5rem] items-center gap-1.5 rounded-full bg-muted px-2 text-xs sm:max-w-[10rem]"
        >
          <Folder aria-hidden className="size-3 shrink-0" />
          <span className="truncate">Workspace: {workspace.display_name}</span>
        </span>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Workspace options"
              className="text-text-tertiary hover:text-text-primary size-5"
            >
              <MoreHorizontal className="size-3.5" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            <DropdownMenuItem onSelect={detach}>Detach workspace</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    )
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon-xs"
          data-attach-folder
          aria-label={attachPending ? 'Attaching…' : 'Attach a folder'}
          className="text-text-tertiary hover:text-text-primary size-6 rounded-full"
          disabled={attachPending}
          onClick={() => void beginAttach()}
        >
          {attachPending ? (
            <Spinner aria-hidden role="presentation" className="size-3.5" />
          ) : (
            <Folder aria-hidden className="size-3.5 shrink-0" />
          )}
        </Button>
      </TooltipTrigger>
      <TooltipContent>Attach a folder for Lyra to work in</TooltipContent>
    </Tooltip>
  )
}

/**
 * The bounded path entry for builds without a native picker (the browser build), shown
 * by the work surface when a just-in-time attach request is approved there. The desktop
 * shell never renders this: its picker returns a real path.
 */
export function AttachPathEntry({
  onSubmit,
  onCancel,
  busy,
}: {
  onSubmit: (rootPath: string) => void
  onCancel: () => void
  busy: boolean
}) {
  const [path, setPath] = useState('')
  return (
    <form
      className="flex flex-col gap-2"
      onSubmit={(event) => {
        event.preventDefault()
        if (!path.trim()) return
        onSubmit(path.trim())
        setPath('')
      }}
    >
      <label htmlFor="agent-attach-path" className="text-sm font-medium">
        Path to the folder
      </label>
      <Input
        id="agent-attach-path"
        value={path}
        placeholder="/absolute/path/to/repository"
        onChange={(event) => setPath(event.target.value)}
      />
      <div className="flex items-center gap-2">
        <Button type="submit" size="sm" disabled={!path.trim() || busy}>
          Attach
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
