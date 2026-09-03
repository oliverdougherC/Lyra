'use client'

import { useState } from 'react'
import { AlertTriangle, Check, ChevronRight, Copy, RefreshCw, X } from 'lucide-react'

import { LyraAvatar } from '@/components/chat/lyra-mark'
import { ReasoningTrace } from '@/components/chat/reasoning-trace'
import { StreamingMarkdown } from '@/components/chat/streaming-markdown'
import {
  stageLabel,
  ThinkingIndicator,
  type ProcessingStage,
} from '@/components/chat/thinking-indicator'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { formatCount, formatRelativeTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { AgentAttempt, TutorAttempt, WriterActivity, WriterAttempt } from '@/types'

export type ChatMessage = {
  id: number
  role: 'user' | 'assistant'
  content: string
  thinking: string
  thinking_ms: number
  retrieval_trimmed: boolean
  omitted_document_count: number
  /** What a writer turn did on the way to this reply. Empty for tutor messages. */
  tool_activity: WriterActivity[]
  created_at: string
  /** The latest agent-turn attempt on this message, when it was an agent turn (PLA-295). */
  agent_attempt?: AgentAttempt | null
  /** The latest writer-turn attempt on this message, when it was a writer turn (PLA-310). */
  writer_attempt?: WriterAttempt | null
  /** The latest tutor-turn attempt on this message, when it was a tutor turn (PLA-306). */
  tutor_attempt?: TutorAttempt | null
}

type MessageRowProps = {
  message: ChatMessage
  /** Spacing supplied by the caller, which is what knows where a turn begins. */
  className?: string
  /** True when a visible gap in time separates this message from the previous one. */
  startsTimeGap?: boolean
  streaming?: boolean
  /**
   * The live activity trail while a writer turn streams; a settled message carries its
   * own in `tool_activity`. Passed separately because the streaming row is a
   * placeholder the pane fills in from frames as they arrive.
   */
  activity?: WriterActivity[]
  /** The stage label to show before any text has arrived. */
  processingStage?: ProcessingStage | null
  /** When the turn started, so the wait can report how long it has run. */
  turnStartedAt?: number | null
  turnEnded?: boolean
  onRevealComplete?: () => void
  canRetry?: boolean
  onRetry?: () => void
}

export function MessageRow({
  message,
  className,
  startsTimeGap,
  streaming,
  activity,
  processingStage,
  turnStartedAt,
  turnEnded,
  onRevealComplete,
  canRetry,
  onRetry,
}: MessageRowProps) {
  if (message.role === 'user') {
    const agentAttempt = message.agent_attempt
    const writerAttempt = message.writer_attempt
    const tutorAttempt = message.tutor_attempt
    const agentFailed = agentAttempt?.state === 'failed' || agentAttempt?.state === 'stopped'
    const writerFailed = writerAttempt?.state === 'failed' || writerAttempt?.state === 'stopped'
    const tutorFailed = tutorAttempt?.state === 'failed' || tutorAttempt?.state === 'stopped'
    return (
      <div className={cn('group flex flex-col items-end', className)}>
        <div className="bg-accent-secondary/45 border-accent-secondary/60 max-w-[80%] rounded-2xl rounded-br-md border px-4 py-2.5 text-[0.9375rem] leading-6 whitespace-pre-wrap">
          {message.content}
        </div>
        {agentFailed ? (
          <AgentTurnFailure detail={agentAttempt?.detail ?? null} onRetry={onRetry} />
        ) : null}
        {writerFailed ? (
          <WriterTurnFailure detail={writerAttempt?.detail ?? null} onRetry={onRetry} />
        ) : null}
        {tutorFailed ? (
          <TutorTurnFailure detail={tutorAttempt?.detail ?? null} onRetry={onRetry} />
        ) : null}
        <MessageActions
          align="end"
          content={message.content}
          createdAt={message.created_at}
          pinned={startsTimeGap}
        />
      </div>
    )
  }

  const hasAnswer = message.content.trim().length > 0
  // Thinking is only "in progress" while nothing has been answered yet: the first word of
  // the reply is what ends it, whichever channel the model is still writing on.
  const thinkingNow = Boolean(streaming) && !hasAnswer && message.thinking.trim().length > 0
  const trail = activity ?? message.tool_activity
  const working = Boolean(streaming) && !hasAnswer && trail.length > 0
  const waiting = Boolean(streaming) && !hasAnswer && !thinkingNow && !working

  return (
    <div className={cn('group flex w-full gap-3', className)}>
      <LyraAvatar thinking={Boolean(streaming) && !hasAnswer} />
      <div className="min-w-0 flex-1">
        {/* Live, the thought and the tool trail are visible while they move. Settled, the
            turn keeps one quiet record of how the answer was made - a single collapsed
            `Details` disclosure, because a `Thought for 6 seconds` line that outlives the
            turn is the machine narrating itself, not the student's task. */}
        {streaming ? (
          <>
            {message.thinking.trim() ? (
              <ReasoningTrace
                text={message.thinking}
                streaming={thinkingNow}
                startedAt={turnStartedAt}
              />
            ) : null}
            {trail.length > 0 ? <ActivityTrail entries={trail} working={working} /> : null}
          </>
        ) : message.thinking.trim() || trail.length > 0 ? (
          <TurnDetails thinking={message.thinking} trail={trail} />
        ) : null}

        {waiting ? (
          <ThinkingIndicator
            label={stageLabel(processingStage ?? null)}
            startedAt={turnStartedAt ?? null}
            className="h-7"
          />
        ) : hasAnswer || !streaming ? (
          <StreamingMarkdown
            content={message.content}
            streaming={streaming}
            turnEnded={turnEnded}
            onRevealComplete={onRevealComplete}
          />
        ) : null}

        {message.retrieval_trimmed ? (
          <RetrievalNotice omittedDocumentCount={message.omitted_document_count} />
        ) : null}

        {streaming ? null : (
          <MessageActions
            align="start"
            content={message.content}
            createdAt={message.created_at}
            pinned={startsTimeGap}
            onRetry={canRetry ? onRetry : undefined}
          />
        )}
      </div>
    </div>
  )
}

/**
 * What a writer turn did on its way to the answer, one quiet line per tool call, in the
 * accent rail that marks Lyra's own work elsewhere. While the turn is running the last
 * line carries the pulse; settled, the trail reads as the reply's provenance. A failed
 * call stays in the trail - the model was told and moved on, and hiding it would make
 * the record a story.
 */
function ActivityTrail({ entries, working }: { entries: WriterActivity[]; working?: boolean }) {
  return (
    <div
      className="border-accent-primary/40 mb-2 flex flex-col gap-1 border-l-2 py-0.5 pl-3"
      aria-label="What Lyra did for this reply"
    >
      {entries.map((entry, index) => {
        const active = Boolean(working) && index === entries.length - 1
        return (
          <div
            key={`${index}-${entry.tool}`}
            className={cn(
              'flex items-center gap-1.5 text-xs',
              active ? 'text-text-secondary' : 'text-text-tertiary',
            )}
          >
            {active ? (
              <span className="bg-accent-primary size-1.5 shrink-0 animate-pulse rounded-full" />
            ) : entry.ok ? (
              <Check className="text-accent-primary/70 size-3 shrink-0" />
            ) : (
              <X className="text-destructive/70 size-3 shrink-0" />
            )}
            <span className="min-w-0 truncate">{entry.label}</span>
          </div>
        )
      })}
    </div>
  )
}

/**
 * The one collapsed record of how a settled answer was made: the reasoning model's thought
 * and the writer's tool trail behind a single `Details` disclosure.
 *
 * While a turn is live, the moving parts are shown - a thought being written, a tool call
 * in flight - because that is what the reader is waiting for. Once the answer has landed,
 * none of it is the task any more. A permanent `Thought for 6 seconds` line is the machine
 * narrating itself, and it sat above the answer in every settled message. The detail stays
 * one click away for anyone who wants to see how it was reached, but the default is the
 * answer, which is what the student came for.
 *
 * A model that neither thinks nor calls tools never renders this at all.
 */
function TurnDetails({ thinking, trail }: { thinking: string; trail: WriterActivity[] }) {
  return (
    <Collapsible className="mb-3">
      <CollapsibleTrigger
        className={cn(
          'group/details -mx-1 flex h-7 items-center gap-1.5 rounded-md px-1 text-text-tertiary',
          'transition-colors hover:text-text-secondary',
          'focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none',
        )}
      >
        <ChevronRight
          aria-hidden
          className="size-3 shrink-0 transition-transform duration-200 group-data-[state=open]/details:rotate-90"
        />
        <span className="text-xs">Details</span>
      </CollapsibleTrigger>
      <CollapsibleContent
        className={cn(
          'overflow-hidden',
          'data-[state=closed]:animate-collapsible-up data-[state=open]:animate-collapsible-down',
        )}
      >
        <div className="mt-2 flex flex-col gap-3">
          {thinking.trim() ? (
            <div
              className={cn(
                'reasoning-body scrollbar-none max-h-[22rem] overflow-y-auto border-l border-border pl-3',
                'font-ai-response text-text-secondary text-[0.9375rem] leading-6',
              )}
            >
              <StreamingMarkdown content={thinking} />
            </div>
          ) : null}
          {trail.length > 0 ? <ActivityTrail entries={trail} /> : null}
        </div>
      </CollapsibleContent>
    </Collapsible>
  )
}

/**
 * The row under a message: timestamp, copy, and on the last reply, retry. Hidden until the
 * message is hovered or something in the row takes focus, so a conversation reads as prose
 * rather than as a list of toolbars.
 */
function MessageActions({
  align,
  content,
  createdAt,
  pinned,
  onRetry,
}: {
  align: 'start' | 'end'
  content: string
  createdAt: string
  pinned?: boolean
  onRetry?: () => void
}) {
  return (
    <div
      className={cn(
        'mt-1 flex h-7 items-center gap-0.5',
        align === 'end' ? 'justify-end' : 'justify-start',
        'opacity-0 transition-opacity duration-150 group-hover:opacity-100 focus-within:opacity-100',
        pinned && 'opacity-100',
      )}
    >
      {pinned ? (
        <span className="text-text-tertiary mr-1 text-xs">{formatRelativeTime(createdAt)}</span>
      ) : null}
      <CopyButton content={content} />
      {onRetry ? (
        <ActionButton label="Try this answer again" onClick={onRetry}>
          <RefreshCw className="size-3.5" />
        </ActionButton>
      ) : null}
      {pinned ? null : (
        <span className="text-text-tertiary ml-1 text-xs">{formatRelativeTime(createdAt)}</span>
      )}
    </div>
  )
}

function ActionButton({
  label,
  onClick,
  children,
}: {
  label: string
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon-sm"
          className="text-text-tertiary hover:text-foreground"
          onClick={onClick}
          aria-label={label}
        >
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}

function CopyButton({ content }: { content: string }) {
  const [copied, setCopied] = useState(false)

  return (
    <ActionButton
      label={copied ? 'Copied' : 'Copy message'}
      onClick={() => {
        void navigator.clipboard.writeText(content).then(() => {
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1500)
        })
      }}
    >
      {copied ? <Check className="size-3.5 text-success-text" /> : <Copy className="size-3.5" />}
    </ActionButton>
  )
}

/**
 * A failed or stopped agent turn, shown under the question it answers. Truthful and
 * bounded: it carries the server's own privacy-safe detail (never an endpoint, path, or
 * transcript) and points at the agent controls, where the turn was sent from and where
 * Retry lives. `data-agent-turn-failure` marks it for the tests that assert the state.
 */
function AgentTurnFailure({ detail, onRetry }: { detail: string | null; onRetry?: () => void }) {
  return (
    <div
      data-agent-turn-failure
      className="text-destructive mt-1 flex max-w-[80%] items-start gap-1.5 text-xs"
      role="status"
    >
      <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
      <span>{detail?.trim() || 'This turn did not finish.'}</span>
      {onRetry ? (
        <Button
          variant="ghost"
          size="icon-sm"
          className="text-destructive hover:text-foreground -mt-0.5 ml-0.5 shrink-0"
          onClick={onRetry}
          aria-label="Try again"
        >
          <RefreshCw className="size-3" />
        </Button>
      ) : null}
    </div>
  )
}

function WriterTurnFailure({ detail, onRetry }: { detail: string | null; onRetry?: () => void }) {
  return (
    <div
      data-writer-turn-failure
      className="text-destructive mt-1 flex max-w-[80%] items-start gap-1.5 text-xs"
      role="status"
    >
      <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
      <span>{detail?.trim() || 'This turn did not finish.'}</span>
      {onRetry ? (
        <Button
          variant="ghost"
          size="icon-sm"
          className="text-destructive hover:text-foreground -mt-0.5 ml-0.5 shrink-0"
          onClick={onRetry}
          aria-label="Try again"
        >
          <RefreshCw className="size-3" />
        </Button>
      ) : null}
    </div>
  )
}

function TutorTurnFailure({ detail, onRetry }: { detail: string | null; onRetry?: () => void }) {
  return (
    <div
      data-tutor-turn-failure
      className="text-destructive mt-1 flex max-w-[80%] items-start gap-1.5 text-xs"
      role="status"
    >
      <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
      <span>{detail?.trim() || 'This turn did not finish.'}</span>
      {onRetry ? (
        <Button
          variant="ghost"
          size="icon-sm"
          className="text-destructive hover:text-foreground -mt-0.5 ml-0.5 shrink-0"
          onClick={onRetry}
          aria-label="Try again"
        >
          <RefreshCw className="size-3" />
        </Button>
      ) : null}
    </div>
  )
}

/**
 * Understated but never hidden: without it, a truncation artifact reads as the model
 * simply being wrong.
 */
export function RetrievalNotice({ omittedDocumentCount }: { omittedDocumentCount: number }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <p className="text-text-tertiary mt-2 w-fit cursor-help text-xs">
          Some material did not fit in the model&apos;s context.
        </p>
      </TooltipTrigger>
      <TooltipContent>
        {omittedDocumentCount > 0
          ? `${formatCount(omittedDocumentCount, 'document')} were left out of this answer.`
          : 'Part of the retrieved material was left out of this answer.'}
      </TooltipContent>
    </Tooltip>
  )
}
