'use client'

import { Check, X } from 'lucide-react'

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
import { formatCount } from '@/lib/format'
import { cn } from '@/lib/utils'

import type { WorkspaceChangeHunk, WorkspaceChangeReview } from './types'

type WorkspaceChangeReviewRailProps = {
  change: WorkspaceChangeReview
  busy?: boolean
  onAcceptHunk?: (hunk: WorkspaceChangeHunk) => void
  onRejectHunk?: (hunk: WorkspaceChangeHunk) => void
  onAcceptAll?: () => void
  onRejectAll?: () => void
}

export function WorkspaceChangeReviewRail({
  change,
  busy,
  onAcceptHunk,
  onRejectHunk,
  onAcceptAll,
  onRejectAll,
}: WorkspaceChangeReviewRailProps) {
  const acceptedCount = change.hunks.filter((hunk) => hunk.decision === 'accepted').length
  const rejectedCount = change.hunks.filter((hunk) => hunk.decision === 'rejected').length
  const remaining = change.hunks.filter((hunk) => !hunk.decision)

  return (
    <Card aria-label={`Workspace change for ${change.path}`}>
      <CardHeader>
        <div className="flex min-w-0 items-start gap-3">
          <div className="min-w-0 flex-1">
            <CardTitle className="break-all font-mono text-sm">{change.path}</CardTitle>
            <CardDescription>
              {change.rationale ?? 'Review the proposed file changes.'}
            </CardDescription>
          </div>
          <CardAction>
            <ChangeStateBadge state={change.state} />
          </CardAction>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {change.summary ? <p className="text-text-secondary text-sm">{change.summary}</p> : null}

        {change.state === 'partially_applied' ? (
          <Alert>
            <AlertTitle>Partial review in progress</AlertTitle>
            <AlertDescription>
              {acceptedCount > 0
                ? `${formatCount(acceptedCount, 'hunk')} accepted.`
                : 'No hunks accepted yet.'}{' '}
              {rejectedCount > 0
                ? `${formatCount(rejectedCount, 'hunk')} rejected.`
                : 'No hunks rejected yet.'}{' '}
              {formatCount(remaining.length, 'hunk')} still need a decision.
            </AlertDescription>
          </Alert>
        ) : null}

        {change.state === 'stale' ? (
          <StaleReview
            currentContent={change.currentContent}
            proposedContent={change.proposedContent}
            busy={busy}
            onAcceptAll={onAcceptAll}
            onRejectAll={onRejectAll}
          />
        ) : (
          <>
            <div className="flex flex-wrap gap-2">
              {onAcceptAll ? (
                <Button size="sm" disabled={busy || remaining.length === 0} onClick={onAcceptAll}>
                  Accept remaining
                </Button>
              ) : null}
              {onRejectAll ? (
                <Button variant="outline" size="sm" disabled={busy} onClick={onRejectAll}>
                  Reject proposal
                </Button>
              ) : null}
            </div>
            <ul className="flex flex-col gap-3" aria-label="Workspace change hunks">
              {change.hunks.map((hunk) => (
                <li key={hunk.hash}>
                  <HunkCard
                    hunk={hunk}
                    busy={busy}
                    onAccept={onAcceptHunk}
                    onReject={onRejectHunk}
                  />
                </li>
              ))}
            </ul>
          </>
        )}
      </CardContent>
    </Card>
  )
}

function StaleReview({
  currentContent,
  proposedContent,
  busy,
  onAcceptAll,
  onRejectAll,
}: {
  currentContent?: string
  proposedContent?: string
  busy?: boolean
  onAcceptAll?: () => void
  onRejectAll?: () => void
}) {
  return (
    <>
      <Alert>
        <AlertTitle>Proposal is stale</AlertTitle>
        <AlertDescription>
          The file changed after this proposal was created. Review the current file beside the
          proposed replacement before accepting or rejecting it.
        </AlertDescription>
      </Alert>
      <div className="grid gap-3 md:grid-cols-2">
        <section aria-label="Current file" className="border-border bg-card rounded-md border p-3">
          <h3 className="text-text-tertiary mb-2 text-xs font-medium tracking-[0.14em] uppercase">
            Current file
          </h3>
          <pre className="text-text-secondary overflow-x-auto text-xs whitespace-pre-wrap">
            {currentContent ?? 'Current file unavailable.'}
          </pre>
        </section>
        <section aria-label="Proposed file" className="border-border bg-card rounded-md border p-3">
          <h3 className="text-text-tertiary mb-2 text-xs font-medium tracking-[0.14em] uppercase">
            Proposed file
          </h3>
          <pre className="text-text-secondary overflow-x-auto text-xs whitespace-pre-wrap">
            {proposedContent ?? 'Proposed file unavailable.'}
          </pre>
        </section>
      </div>
      <div className="flex flex-wrap gap-2">
        {onAcceptAll ? (
          <Button size="sm" disabled={busy} onClick={onAcceptAll}>
            Replace file with proposal
          </Button>
        ) : null}
        {onRejectAll ? (
          <Button variant="outline" size="sm" disabled={busy} onClick={onRejectAll}>
            Reject proposal
          </Button>
        ) : null}
      </div>
    </>
  )
}

function HunkCard({
  hunk,
  busy,
  onAccept,
  onReject,
}: {
  hunk: WorkspaceChangeHunk
  busy?: boolean
  onAccept?: (hunk: WorkspaceChangeHunk) => void
  onReject?: (hunk: WorkspaceChangeHunk) => void
}) {
  const settled = Boolean(hunk.decision)

  return (
    <article className="border-border bg-card overflow-hidden rounded-md border">
      <header className="border-border flex items-center justify-between gap-2 border-b px-3 py-2">
        <div className="min-w-0">
          <p className="text-sm font-medium">Change {hunk.index + 1}</p>
          {hunk.header ? (
            <p className="text-text-tertiary truncate font-mono text-xs">{hunk.header}</p>
          ) : null}
        </div>
        {hunk.decision ? (
          <Badge variant={hunk.decision === 'accepted' ? 'secondary' : 'destructive'}>
            {hunk.decision === 'accepted' ? 'Accepted' : 'Rejected'}
          </Badge>
        ) : null}
      </header>
      <div className="font-mono text-xs leading-relaxed">
        {hunk.lines.map((line, index) => {
          const sign = line.charAt(0)
          const text = line.slice(1)
          return (
            <div
              key={index}
              className={cn(
                'flex gap-2 px-2 py-0.5',
                sign === '-' && 'bg-danger-fill text-danger-text',
                sign === '+' && 'bg-success-fill text-success-text',
                sign === ' ' && 'text-text-tertiary',
              )}
            >
              <span className="w-3 shrink-0 text-center select-none" aria-hidden>
                {sign === ' ' ? '' : sign}
              </span>
              <span className="break-words whitespace-pre-wrap">{text}</span>
            </div>
          )
        })}
      </div>
      <footer className="border-border flex gap-1 border-t px-2 py-1.5">
        <Button
          variant="ghost"
          size="icon-sm"
          className="shrink-0"
          aria-label={`Accept change ${hunk.index + 1}`}
          disabled={busy || settled || !onAccept}
          onClick={() => onAccept?.(hunk)}
        >
          <Check className="size-3.5" />
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          className="shrink-0"
          aria-label={`Reject change ${hunk.index + 1}`}
          disabled={busy || settled || !onReject}
          onClick={() => onReject?.(hunk)}
        >
          <X className="size-3.5" />
        </Button>
      </footer>
    </article>
  )
}

function ChangeStateBadge({ state }: { state: WorkspaceChangeReview['state'] }) {
  return (
    <Badge
      variant={
        state === 'applied'
          ? 'secondary'
          : state === 'rejected' || state === 'failed'
            ? 'destructive'
            : 'outline'
      }
    >
      {state === 'partially_applied'
        ? 'Partially applied'
        : state === 'stale'
          ? 'Stale'
          : state === 'applied'
            ? 'Applied'
            : state === 'rejected'
              ? 'Rejected'
              : state === 'failed'
                ? 'Failed'
                : 'Pending'}
    </Badge>
  )
}
