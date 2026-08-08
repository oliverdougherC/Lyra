'use client'

import { Globe2, LockKeyhole, ShieldAlert, TerminalSquare } from 'lucide-react'

import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { cn } from '@/lib/utils'

import type { AgentGrantKey, AgentGrantState, WorkspaceAttachment } from './types'

type CapabilitySummaryProps = {
  workspace?: WorkspaceAttachment | null
  grants: AgentGrantState[]
  busy?: boolean
  onToggleGrant?: (key: AgentGrantKey, nextEnabled: boolean) => void
}

const GRANT_ICONS = {
  web: Globe2,
  workspace_read: LockKeyhole,
  change_proposals: ShieldAlert,
  commands: TerminalSquare,
} as const

const GRANT_ORDER: AgentGrantKey[] = ['web', 'workspace_read', 'change_proposals', 'commands']

export function CapabilitySummary({
  workspace,
  grants,
  busy,
  onToggleGrant,
}: CapabilitySummaryProps) {
  const ordered = [...grants].sort(
    (a, b) => GRANT_ORDER.indexOf(a.key) - GRANT_ORDER.indexOf(b.key),
  )

  return (
    <section className="flex flex-col gap-4" aria-label="Agent capabilities">
      <Card>
        <CardHeader>
          <CardTitle>Attached workspace</CardTitle>
          <CardDescription>
            Workspace attachment identifies the root only. It does not grant read, proposal, or
            command access on its own.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {workspace ? (
            <>
              <div className="flex flex-wrap items-start gap-2">
                <Badge variant="outline">Attached</Badge>
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium">{workspace.label}</p>
                  <p className="text-text-tertiary break-all font-mono text-xs">
                    {workspace.rootPath}
                  </p>
                </div>
              </div>
              {workspace.helperText ? (
                <p className="text-text-secondary text-sm">{workspace.helperText}</p>
              ) : null}
            </>
          ) : (
            <Alert>
              <AlertTitle>No workspace attached</AlertTitle>
              <AlertDescription>
                Web research can still be granted independently. Workspace read, change proposals,
                and commands remain unavailable until a root is attached.
              </AlertDescription>
            </Alert>
          )}

          <p className="text-text-tertiary text-xs">
            Attaching a workspace enables none of the grants below.
          </p>
        </CardContent>
      </Card>

      <div className="grid gap-3 md:grid-cols-2">
        {ordered.map((grant) => (
          <GrantCard key={grant.key} grant={grant} busy={busy} onToggle={onToggleGrant} />
        ))}
      </div>
    </section>
  )
}

function GrantCard({
  grant,
  busy,
  onToggle,
}: {
  grant: AgentGrantState
  busy?: boolean
  onToggle?: (key: AgentGrantKey, nextEnabled: boolean) => void
}) {
  const Icon = GRANT_ICONS[grant.key]
  const actionDisabled = busy || grant.unavailable || Boolean(grant.blockedReason && !grant.enabled)

  return (
    <Card size="sm">
      <CardHeader>
        <div className="flex items-start gap-2">
          <span className="bg-muted text-text-secondary rounded-md p-2" aria-hidden>
            <Icon className="size-4" />
          </span>
          <div className="min-w-0 flex-1">
            <CardTitle className="text-sm">{grant.label}</CardTitle>
            <CardDescription>{grant.description}</CardDescription>
          </div>
        </div>
        <CardAction>
          <GrantBadge grant={grant} />
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {grant.blockedReason ? (
          <p className="text-text-secondary text-sm">{grant.blockedReason}</p>
        ) : null}
        {grant.note ? <p className="text-text-secondary text-sm">{grant.note}</p> : null}
        {grant.pendingInvalidationCount ? (
          <p className="text-text-tertiary text-xs">
            Revoking this grant invalidates {grant.pendingInvalidationCount}{' '}
            {grant.pendingInvalidationCount === 1 ? 'pending request' : 'pending requests'}.
          </p>
        ) : null}
        {onToggle ? (
          <div className="flex items-center justify-between gap-3">
            <span
              className={cn(
                'text-xs',
                grant.enabled ? 'text-text-secondary' : 'text-text-tertiary',
              )}
            >
              {grant.enabled ? 'Granted for this class' : 'Not granted for this class'}
            </span>
            <Button
              variant={grant.enabled ? 'outline' : 'default'}
              size="sm"
              disabled={actionDisabled}
              aria-label={`${grant.enabled ? 'Disable' : 'Enable'} ${grant.label}`}
              onClick={() => onToggle(grant.key, !grant.enabled)}
            >
              {grant.enabled ? 'Disable' : 'Enable'}
            </Button>
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}

function GrantBadge({ grant }: { grant: AgentGrantState }) {
  if (grant.unavailable) return <Badge variant="outline">Unavailable</Badge>
  if (grant.revoked) return <Badge variant="destructive">Revoked</Badge>
  if (grant.inherited) {
    return (
      <Badge variant={grant.enabled ? 'secondary' : 'outline'}>
        {grant.enabled ? 'Inherited on' : 'Inherited off'}
      </Badge>
    )
  }
  return (
    <Badge variant={grant.enabled ? 'secondary' : 'outline'}>{grant.enabled ? 'On' : 'Off'}</Badge>
  )
}
