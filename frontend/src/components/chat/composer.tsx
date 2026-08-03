'use client'

import Link from 'next/link'
import { useCallback, useLayoutEffect, useRef } from 'react'
import { ArrowUp, Square, X } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Kbd } from '@/components/ui/kbd'
import { Textarea } from '@/components/ui/textarea'
import { useLocalStorageState } from '@/lib/hooks/use-local-storage-state'

const MAX_ROWS = 3
const LINE_HEIGHT_PX = 24
const HINT_KEY = 'lyra-composer-hint-dismissed'

// Hoisted so the snapshot reader inside the hook keeps a stable identity across renders.
const parseDismissed = (raw: string): boolean => raw === 'true'

type ComposerProps = {
  value: string
  onChange: (value: string) => void
  onSend: () => void
  onStop: () => void
  streaming: boolean
  /** Non-null disables the composer and explains why. */
  disabledReason: string | null
  scopedDocumentName: string | null
  onClearScope: () => void
}

export function Composer({
  value,
  onChange,
  onSend,
  onStop,
  streaming,
  disabledReason,
  scopedDocumentName,
  onClearScope,
}: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const [hintDismissed, setHintDismissed] = useLocalStorageState(HINT_KEY, false, parseDismissed)

  // Auto-grow up to three rows, then let the textarea scroll internally.
  useLayoutEffect(() => {
    const node = textareaRef.current
    if (!node) return
    node.style.height = 'auto'
    node.style.height = `${Math.min(node.scrollHeight, MAX_ROWS * LINE_HEIGHT_PX + 16)}px`
  }, [value])

  const send = useCallback(() => {
    if (streaming || disabledReason || value.trim().length === 0) return
    if (!hintDismissed) setHintDismissed(true)
    onSend()
  }, [streaming, disabledReason, value, hintDismissed, setHintDismissed, onSend])

  if (disabledReason) {
    return (
      <div className="rounded-md border border-border-strong bg-muted p-3 text-sm">
        <p className="text-text-secondary">{disabledReason}</p>
        <Button asChild variant="outline" size="sm" className="mt-2">
          <Link href="/settings">Open settings</Link>
        </Button>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      {scopedDocumentName ? (
        <Badge variant="secondary" className="gap-1">
          Only {scopedDocumentName}
          <button
            type="button"
            onClick={onClearScope}
            className="focus-visible:ring-ring/50 rounded-full focus-visible:ring-[2px] focus-visible:outline-none"
          >
            <X className="size-3" />
            <span className="sr-only">Search all documents again</span>
          </button>
        </Badge>
      ) : null}

      <div className="border-input focus-within:border-accent-primary flex items-end gap-2 rounded-md border bg-card p-2 shadow-sm transition-colors focus-within:ring-2 focus-within:ring-ring/20">
        <Textarea
          ref={textareaRef}
          rows={1}
          value={value}
          placeholder="Ask about your material"
          aria-label="Message Lyra"
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              send()
            }
          }}
          className="max-h-[88px] min-h-0 resize-none border-0 bg-transparent p-1 shadow-none focus-visible:ring-0"
        />
        {streaming ? (
          <Button
            size="icon"
            variant="outline"
            className="shrink-0 rounded-full"
            onClick={onStop}
            aria-label="Stop generating"
          >
            <Square className="size-3.5" />
          </Button>
        ) : (
          <Button
            size="icon"
            className="shrink-0 rounded-full"
            onClick={send}
            disabled={value.trim().length === 0}
            aria-label="Send message"
          >
            <ArrowUp />
          </Button>
        )}
      </div>

      {hintDismissed ? null : (
        <p className="text-text-tertiary text-xs">
          <Kbd>Enter</Kbd> sends, <Kbd>Shift</Kbd> <Kbd>Enter</Kbd> starts a new line.
        </p>
      )}
    </div>
  )
}
