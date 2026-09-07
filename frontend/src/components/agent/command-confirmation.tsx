'use client'

import { AlertTriangle, TerminalSquare } from 'lucide-react'

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

import type { AgentCommandRequest } from './types'

type CommandConfirmationCardProps = {
  command: AgentCommandRequest
  busy?: boolean
  onConfirm?: () => void
  onReject?: () => void
}

export function CommandConfirmationCard({
  command,
  busy,
  onConfirm,
  onReject,
}: CommandConfirmationCardProps) {
  const confirmDisabled =
    busy || command.state !== 'pending' || Boolean(command.unavailableReason) || !onConfirm

  return (
    <Card className="@container" aria-label={`Command request ${command.id}`}>
      <CardHeader>
        <div className="flex min-w-0 items-start gap-3">
          <div className="bg-muted text-text-secondary rounded-md p-2" aria-hidden>
            <TerminalSquare className="size-4" />
          </div>
          <div className="min-w-0 flex-1">
            <CardTitle className="text-sm">Verification command</CardTitle>
            <CardDescription>{command.reason}</CardDescription>
          </div>
          <CardAction>
            <CommandStateBadge state={command.state} />
          </CardAction>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="grid gap-3 @min-[42rem]:grid-cols-[minmax(0,2fr)_minmax(14rem,1fr)]">
          <section
            aria-label="Command to run"
            className="border-border bg-card rounded-md border p-3"
          >
            <h3 className="text-text-tertiary mb-2 text-xs font-medium tracking-[0.14em] uppercase">
              Command to run
            </h3>
            <pre className="overflow-x-auto whitespace-pre-wrap break-all font-mono text-sm leading-6">
              {command.argv
                .map((arg) => (/^[a-zA-Z0-9_./:=+-]+$/.test(arg) ? arg : JSON.stringify(arg)))
                .join(' ')}
            </pre>
            <details className="mt-3 text-xs">
              <summary className="cursor-pointer text-text-secondary">Argument details</summary>
              <p className="my-2 text-text-secondary">
                Each argument is passed separately, without shell expansion.
              </p>
              <ol className="flex flex-wrap gap-2" role="list" aria-label="Command arguments">
                {command.argv.map((arg, index) => (
                  <li
                    key={`${index}-${arg}`}
                    className="bg-muted rounded-md px-2 py-1 font-mono text-sm leading-6"
                  >
                    <span className="text-text-tertiary mr-1">{index}:</span>
                    <span>{arg}</span>
                  </li>
                ))}
              </ol>
            </details>
          </section>

          <section className="border-border bg-card rounded-md border p-3">
            <h3 className="text-text-tertiary mb-2 text-xs font-medium tracking-[0.14em] uppercase">
              Runtime
            </h3>
            <dl className="grid gap-2 text-sm">
              <div>
                <dt className="text-text-tertiary text-xs">Working directory</dt>
                <dd className="break-all font-mono text-sm leading-6">{command.cwd}</dd>
              </div>
              <div>
                <dt className="text-text-tertiary text-xs">Timeout</dt>
                <dd>{command.timeoutSeconds} seconds</dd>
              </div>
              {command.expectedSignal ? (
                <div>
                  <dt className="text-text-tertiary text-xs">Expected result</dt>
                  <dd>{command.expectedSignal}</dd>
                </div>
              ) : null}
              {command.confirmedAtLabel ? (
                <div>
                  <dt className="text-text-tertiary text-xs">Confirmed</dt>
                  <dd>{command.confirmedAtLabel}</dd>
                </div>
              ) : null}
            </dl>
          </section>
        </div>

        <Alert>
          <AlertTriangle className="size-4" />
          <AlertTitle>Runs exactly as shown</AlertTitle>
          <AlertDescription>
            This runs code from your attached folder and may change files. Review the command and
            working folder before running it.
          </AlertDescription>
        </Alert>

        {command.networkRisk !== 'none' ? (
          <Alert variant="destructive">
            <AlertTitle>Network effects are possible</AlertTitle>
            <AlertDescription>
              This command may contact package registries, remote services, or course endpoints
              depending on the repository tooling. Confirm only if that risk is acceptable.
            </AlertDescription>
          </Alert>
        ) : null}

        {command.unavailableReason ? (
          <Alert>
            <AlertTitle>Command cannot be confirmed yet</AlertTitle>
            <AlertDescription>{command.unavailableReason}</AlertDescription>
          </Alert>
        ) : null}

        <div className="flex flex-wrap gap-2">
          {onConfirm ? (
            <Button size="sm" disabled={confirmDisabled} onClick={onConfirm}>
              Confirm and run
            </Button>
          ) : null}
          {onReject ? (
            <Button
              variant="outline"
              size="sm"
              disabled={busy || command.state !== 'pending'}
              onClick={onReject}
            >
              Reject command
            </Button>
          ) : null}
        </div>

        <CommandOutput command={command} />
      </CardContent>
    </Card>
  )
}

function CommandOutput({ command }: { command: AgentCommandRequest }) {
  const hasOutput = Boolean(command.stdout || command.stderr)

  return (
    <section className="flex flex-col gap-3" aria-label="Command output">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-medium">Output</h3>
        {command.exitCode != null ? <Badge variant="outline">Exit {command.exitCode}</Badge> : null}
        {command.truncated ? <Badge variant="outline">Output truncated</Badge> : null}
      </div>

      {command.state === 'pending' && !hasOutput ? (
        <p className="text-text-tertiary text-sm">Nothing has run yet.</p>
      ) : null}
      {command.state === 'running' && !hasOutput ? (
        <p className="text-text-tertiary text-sm">Waiting for output.</p>
      ) : null}

      {hasOutput ? (
        <div className="grid gap-3 @min-[42rem]:grid-cols-2">
          <OutputPane label="Output" content={command.stdout} />
          <OutputPane label="Errors" content={command.stderr} />
        </div>
      ) : null}
    </section>
  )
}

function OutputPane({ label, content }: { label: string; content?: string }) {
  return (
    <section className="border-border bg-card rounded-md border p-3" aria-label={label}>
      <h4 className="text-text-tertiary mb-2 text-xs font-medium tracking-[0.14em] uppercase">
        {label}
      </h4>
      <pre className="overflow-x-auto font-mono text-sm leading-6 whitespace-pre-wrap">
        {content && content.length > 0 ? content : 'No output.'}
      </pre>
    </section>
  )
}

export function CommandStateBadge({ state }: { state: AgentCommandRequest['state'] }) {
  return (
    <Badge
      variant={
        state === 'completed'
          ? 'secondary'
          : state === 'failed' || state === 'timed_out' || state === 'rejected'
            ? 'destructive'
            : 'outline'
      }
    >
      {state === 'timed_out'
        ? 'Timed out'
        : state === 'completed'
          ? 'Completed'
          : state === 'failed'
            ? 'Failed'
            : state === 'running'
              ? 'Running'
              : state === 'rejected'
                ? 'Rejected'
                : state === 'abandoned'
                  ? 'Abandoned'
                  : 'Pending'}
    </Badge>
  )
}
