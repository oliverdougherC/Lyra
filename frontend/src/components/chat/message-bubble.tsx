'use client'

import { useState } from 'react'
import { Check, Copy, RefreshCw, X } from 'lucide-react'

import { LyraAvatar } from '@/components/chat/lyra-mark'
import { ReasoningTrace } from '@/components/chat/reasoning-trace'
import { StreamingMarkdown } from '@/components/chat/streaming-markdown'
import {
  stageLabel,
  ThinkingIndicator,
  type ProcessingStage,
} from '@/components/chat/thinking-indicator'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { formatCount, formatRelativeTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { WriterActivity } from '@/types'

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
  /**
   * How long the model spent thinking on the turn currently streaming. A message read back
   * from the server carries its own `thinking_ms` instead, which is what this falls back to.
   */
  thinkingDurationMs?: number | null
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
  thinkingDurationMs,
  turnEnded,
  onRevealComplete,
  canRetry,
  onRetry,
}: MessageRowProps) {
  if (message.role === 'user') {
    return (
      <div className={cn('group flex flex-col items-end', className)}>
        {/* The student's note in the margin: warm tan rather than neutral gray, with one
            square corner pointing back at the composer it came from. */}
        <div className="bg-accent-secondary/45 border-accent-secondary/60 max-w-[80%] rounded-2xl rounded-br-md border px-4 py-2.5 text-[0.9375rem] leading-6 whitespace-pre-wrap">
          {message.content}
        </div>
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
        {message.thinking.trim() ? (
          <ReasoningTrace
            text={message.thinking}
            streaming={thinkingNow}
            startedAt={turnStartedAt}
            durationMs={thinkingDurationMs ?? (message.thinking_ms || null)}
          />
        ) : null}

        {trail.length > 0 ? <ActivityTrail entries={trail} working={working} /> : null}

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
