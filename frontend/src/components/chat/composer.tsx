'use client'

import Link from 'next/link'
import { useCallback, useLayoutEffect, useRef } from 'react'
import { ArrowUp, Square, X } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Kbd } from '@/components/ui/kbd'
import { Textarea } from '@/components/ui/textarea'
import { useLocalStorageState } from '@/lib/hooks/use-local-storage-state'
import { cn } from '@/lib/utils'

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
  const hasDraft = value.trim().length > 0

  if (disabledReason) {
    return (
      <div className="rounded-[10px] border border-border-strong bg-muted p-3 text-sm">
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
        <Badge variant="secondary" className="max-w-full">
          <span className="truncate">Only {scopedDocumentName}</span>
          <button
            type="button"
            onClick={onClearScope}
            aria-label="Clear document scope"
            className="rounded-full focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
          >
            <X aria-hidden className="size-3" />
          </button>
        </Badge>
      ) : null}

      <div className="flex items-end gap-2 rounded-[10px] border border-input bg-card p-2 shadow-sm transition-colors focus-within:border-accent-primary focus-within:ring-2 focus-within:ring-ring/20">
        <Textarea
          ref={textareaRef}
          rows={1}
          id="message-composer"
          name="message"
          value={value}
          disabled={streaming}
          placeholder="Ask about your material"
          aria-label="Message Lyra"
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              send()
            }
          }}
          className="max-h-[88px] min-h-0 flex-1 resize-none border-0 bg-transparent px-1.5 py-1.5 leading-6 shadow-none focus-visible:ring-0 disabled:cursor-text disabled:bg-transparent disabled:opacity-100"
        />

        {streaming ? (
          <Button
            type="button"
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
            type="button"
            size="icon"
            variant={hasDraft ? 'default' : 'secondary'}
            className={cn('shrink-0 rounded-full', !hasDraft && 'text-text-tertiary')}
            onClick={send}
            disabled={!hasDraft}
            aria-label="Send message"
          >
            <ArrowUp />
          </Button>
        )}
      </div>

      {/* Shown until the first message is sent, and never on a phone, where there is no
          physical Enter key to explain and the row costs real reading height. */}
      {hintDismissed ? null : (
        <p className="text-text-tertiary hidden text-xs sm:block">
          <Kbd>Enter</Kbd> sends, <Kbd>Shift</Kbd> <Kbd>Enter</Kbd> starts a new line.
        </p>
      )}
    </div>
  )
}
