'use client'

import { useState } from 'react'
import { Check, Copy, RotateCw, Sparkles } from 'lucide-react'

import { StreamingMarkdown } from '@/components/chat/streaming-markdown'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { formatCount, formatRelativeTime } from '@/lib/format'

export type ChatMessage = {
  id: number
  role: 'user' | 'assistant'
  content: string
  retrieval_trimmed: boolean
  omitted_document_count: number
  created_at: string
}

type MessageRowProps = {
  message: ChatMessage
  streaming?: boolean
  canRetry?: boolean
  onRetry?: () => void
}

export function MessageRow({ message, streaming, canRetry, onRetry }: MessageRowProps) {
  if (message.role === 'user') {
    return (
      <div className="group flex justify-end">
        <div className="max-w-[85%]">
          <div className="rounded-md border bg-muted px-3 py-2 text-sm leading-6 whitespace-pre-wrap">
            {message.content}
          </div>
          <div className="mt-1 flex items-center justify-end gap-1">
            <Timestamp value={message.created_at} />
            <CopyButton content={message.content} />
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="group">
      <div className="flex w-full gap-3 rounded-lg border bg-background p-4">
        <div
          className="bg-accent-surface text-accent-surface-foreground flex size-6 shrink-0 items-center justify-center rounded-full"
          aria-hidden
        >
          <Sparkles className="size-3.5" />
        </div>
        <div className="min-w-0 flex-1">
          <StreamingMarkdown content={message.content} streaming={streaming} />
          {message.retrieval_trimmed ? (
            <RetrievalNotice omittedDocumentCount={message.omitted_document_count} />
          ) : null}
          <div className="mt-1 flex items-center gap-1">
            <Timestamp value={message.created_at} />
            <CopyButton content={message.content} />
            {canRetry && onRetry ? (
              <Button
                variant="ghost"
                size="sm"
                className="h-6 px-2 opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
                onClick={onRetry}
              >
                <RotateCw className="size-3" />
                Retry
              </Button>
            ) : null}
          </div>
        </div>
      </div>
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

function Timestamp({ value }: { value: string }) {
  return (
    <span className="text-text-tertiary text-xs opacity-0 transition-opacity group-hover:opacity-100">
      {formatRelativeTime(value)}
    </span>
  )
}

function CopyButton({ content }: { content: string }) {
  const [copied, setCopied] = useState(false)

  return (
    <Button
      variant="ghost"
      size="sm"
      className="h-6 px-2 opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
      onClick={async () => {
        await navigator.clipboard.writeText(content)
        setCopied(true)
        window.setTimeout(() => setCopied(false), 1500)
      }}
    >
      {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
      <span className="sr-only">Copy message</span>
    </Button>
  )
}
