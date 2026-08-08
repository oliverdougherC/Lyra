'use client'

import { BookOpen, ExternalLink, FileCode2, Globe2, TerminalSquare } from 'lucide-react'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

import type { AgentProposalReference, AgentSourceReference, AgentToolActivity } from './types'

type AgentActivityFeedProps = {
  entries: AgentToolActivity[]
  emptyMessage?: string
}

export function AgentActivityFeed({
  entries,
  emptyMessage = 'No tool activity yet.',
}: AgentActivityFeedProps) {
  if (entries.length === 0) {
    return (
      <Card>
        <CardContent className="py-6">
          <p className="text-text-tertiary text-sm">{emptyMessage}</p>
        </CardContent>
      </Card>
    )
  }

  return (
    <section className="flex flex-col gap-3" aria-label="Agent activity">
      {entries.map((entry) => (
        <ToolActivityCard key={entry.id} entry={entry} />
      ))}
    </section>
  )
}

export function ToolActivityCard({ entry }: { entry: AgentToolActivity }) {
  return (
    <Card size="sm" aria-label={entry.title}>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <CardTitle className="text-sm">{entry.title}</CardTitle>
              <StatusBadge status={entry.status} />
              {entry.truncated ? <Badge variant="outline">Truncated</Badge> : null}
            </div>
            <CardDescription>{entry.toolLabel}</CardDescription>
          </div>
          {entry.timestampLabel ? (
            <span className="text-text-tertiary text-xs">{entry.timestampLabel}</span>
          ) : null}
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {entry.summary ? <p className="text-text-secondary text-sm">{entry.summary}</p> : null}
        {entry.targetLabel ? (
          <p className="text-text-tertiary break-all font-mono text-xs">{entry.targetLabel}</p>
        ) : null}

        {entry.status === 'failed' && entry.failureReason ? (
          <Alert variant="destructive">
            <AlertTitle>Tool failed</AlertTitle>
            <AlertDescription>{entry.failureReason}</AlertDescription>
          </Alert>
        ) : null}

        {(entry.status === 'disabled' || entry.status === 'unavailable') && entry.disabledReason ? (
          <Alert>
            <AlertTitle>
              {entry.status === 'disabled' ? 'Disabled by policy' : 'Temporarily unavailable'}
            </AlertTitle>
            <AlertDescription>{entry.disabledReason}</AlertDescription>
          </Alert>
        ) : null}

        <div className="grid gap-3 md:grid-cols-2">
          {entry.source ? <SourceEvidenceCard source={entry.source} /> : null}
          {entry.proposal ? <ProposalSummaryCard proposal={entry.proposal} /> : null}
        </div>
      </CardContent>
    </Card>
  )
}

export function SourceEvidenceCard({ source }: { source: AgentSourceReference }) {
  return (
    <div className="border-border rounded-md border p-3" aria-label={`Source ${source.title}`}>
      <div className="flex items-start gap-2">
        {source.kind === 'web' ? (
          <Globe2 className="text-text-tertiary mt-0.5 size-4 shrink-0" aria-hidden />
        ) : (
          <BookOpen className="text-text-tertiary mt-0.5 size-4 shrink-0" aria-hidden />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-medium">{source.title}</p>
            <Badge variant="outline">
              {source.kind === 'web' ? 'Web source' : 'Course source'}
            </Badge>
            {source.truncated ? <Badge variant="outline">Excerpt truncated</Badge> : null}
          </div>
          <p className="text-text-tertiary text-xs">Source {source.id}</p>
          {source.detail ? (
            <p className="text-text-secondary mt-1 text-sm">{source.detail}</p>
          ) : null}
        </div>
        {source.href ? (
          <Button asChild variant="ghost" size="icon-sm">
            <a
              href={source.href}
              target="_blank"
              rel="noreferrer"
              aria-label={`Open ${source.title}`}
            >
              <ExternalLink className="size-3.5" />
            </a>
          </Button>
        ) : null}
      </div>
    </div>
  )
}

export function ProposalSummaryCard({ proposal }: { proposal: AgentProposalReference }) {
  return (
    <div className="border-border rounded-md border p-3" aria-label={`Proposal ${proposal.title}`}>
      <div className="flex items-start gap-2">
        {proposal.kind === 'command_request' ? (
          <TerminalSquare className="text-text-tertiary mt-0.5 size-4 shrink-0" aria-hidden />
        ) : (
          <FileCode2 className="text-text-tertiary mt-0.5 size-4 shrink-0" aria-hidden />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-medium">{proposal.title}</p>
            <ProposalStateBadge state={proposal.state} />
          </div>
          <p className="text-text-tertiary text-xs">{proposalKindLabel(proposal.kind)}</p>
        </div>
      </div>
    </div>
  )
}

function StatusBadge({ status }: { status: AgentToolActivity['status'] }) {
  return (
    <Badge
      variant={
        status === 'completed' ? 'secondary' : status === 'failed' ? 'destructive' : 'outline'
      }
      className={cn(status === 'running' && 'animate-pulse')}
    >
      {status === 'completed'
        ? 'Completed'
        : status === 'failed'
          ? 'Failed'
          : status === 'running'
            ? 'Running'
            : status === 'disabled'
              ? 'Disabled'
              : 'Unavailable'}
    </Badge>
  )
}

function ProposalStateBadge({ state }: { state: AgentProposalReference['state'] }) {
  return (
    <Badge
      variant={
        state === 'applied' || state === 'confirmed'
          ? 'secondary'
          : state === 'failed' || state === 'rejected'
            ? 'destructive'
            : 'outline'
      }
    >
      {proposalStateLabel(state)}
    </Badge>
  )
}

function proposalKindLabel(kind: AgentProposalReference['kind']) {
  switch (kind) {
    case 'profile_fact':
      return 'Profile proposal'
    case 'workspace_change':
      return 'Workspace change'
    case 'command_request':
      return 'Command request'
    case 'draft_comment':
      return 'Draft comment'
  }
}

function proposalStateLabel(state: AgentProposalReference['state']) {
  switch (state) {
    case 'pending':
      return 'Pending'
    case 'partially_applied':
      return 'Partially applied'
    case 'applied':
      return 'Applied'
    case 'rejected':
      return 'Rejected'
    case 'stale':
      return 'Stale'
    case 'failed':
      return 'Failed'
    case 'confirmed':
      return 'Confirmed'
  }
}
