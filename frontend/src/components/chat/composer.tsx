'use client'

import Link from '@/router/link'
import { useCallback, useEffect, useLayoutEffect, useRef } from 'react'
import { ArrowUp, Square, X } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Kbd } from '@/components/ui/kbd'
import { Textarea } from '@/components/ui/textarea'
import { useLocalStorageState } from '@/lib/hooks/use-local-storage-state'
import { cn } from '@/lib/utils'

const MAX_ROWS = 6
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
  /** Take focus on mount, for a composer the reader has just opened deliberately. */
  autoFocus?: boolean
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
  autoFocus = false,
}: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  // Set on a keyboard send while the textarea owns focus: `disabled={streaming}` makes
  // the browser blur the control, and nothing else would give focus back when the turn
  // settles -- leaving a keyboard-only student stranded after every Enter-send.
  const restoreFocusAfterStream = useRef(false)
  const [hintDismissed, setHintDismissed] = useLocalStorageState(HINT_KEY, false, parseDismissed)

  // `preventScroll` because the caller places this composer itself. Letting the browser
  // scroll to it would fight that and land somewhere neither of them chose.
  useEffect(() => {
    if (autoFocus) textareaRef.current?.focus({ preventScroll: true })
  }, [autoFocus])

  // When streaming ends, return focus to the textarea a keyboard send took it from --
  // unless the student deliberately focused another interactive control meanwhile
  // (then stealing focus back would be worse than the original loss).
  useEffect(() => {
    if (streaming) return
    if (!restoreFocusAfterStream.current) return
    restoreFocusAfterStream.current = false
    const node = textareaRef.current
    if (!node || node.disabled) return
    const active = document.activeElement
    if (active !== null && active !== document.body && active !== node) return
    node.focus({ preventScroll: true })
  }, [streaming])

  // Auto-grow up to three rows, then let the textarea scroll internally.
  useLayoutEffect(() => {
    const node = textareaRef.current
    if (!node) return
    node.style.height = 'auto'
    node.style.height = `${Math.min(node.scrollHeight, MAX_ROWS * LINE_HEIGHT_PX + 16)}px`
  }, [value])

  const send = useCallback(
    (options?: { restoreFocusAfterStream?: boolean }) => {
      if (streaming || disabledReason || value.trim().length === 0) return
      // Each send owns the flag outright: a mouse send clears any leftover from an
      // earlier keyboard send whose turn never actually streamed.
      restoreFocusAfterStream.current = options?.restoreFocusAfterStream ?? false
      if (!hintDismissed) setHintDismissed(true)
      onSend()
    },
    [streaming, disabledReason, value, hintDismissed, setHintDismissed, onSend],
  )
  const hasDraft = value.trim().length > 0

  if (disabledReason) {
    return (
      <div className="min-h-[var(--pane-control-row)] rounded-2xl border border-border-strong bg-muted p-4 text-sm">
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

      {/* A sheet of writing paper laid on the canvas: one row, the send control riding the
          last line of type. The lift comes from the surface being genuinely a different
          paper from the pane behind it, not from a heavier border. The shared control-row
          height keeps this and the documents well across the seam on one line. */}
      <div
        className={cn(
          'flex min-h-[var(--pane-control-row)] items-end gap-2 rounded-2xl bg-card p-2.5',
          'border border-border/80 shadow-sm transition-[border-color,box-shadow] duration-200',
          'focus-within:border-accent-primary/60 focus-within:shadow-md',
        )}
      >
        <Textarea
          ref={textareaRef}
          rows={1}
          id="message-composer"
          name="message"
          value={value}
          disabled={streaming}
          placeholder="Ask about your material…"
          aria-label="Message Lyra"
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              // Keyboard send from the focused textarea: restore focus after the
              // streaming turn. Button sends never set this, so a mouse send can
              // never steal focus back.
              send({ restoreFocusAfterStream: true })
            }
          }}
          className="max-h-[160px] min-h-0 flex-1 resize-none border-0 bg-transparent px-2 py-1.5 text-[0.9375rem] leading-6 shadow-none focus-visible:ring-0 disabled:cursor-text disabled:bg-transparent disabled:opacity-100"
        />

        {streaming ? (
          <Button
            type="button"
            size="icon-sm"
            variant="outline"
            className="size-9 shrink-0 rounded-full"
            onClick={onStop}
            aria-label="Stop generating"
          >
            <Square className="size-3" />
          </Button>
        ) : (
          <Button
            type="button"
            size="icon-sm"
            variant={hasDraft ? 'default' : 'ghost'}
            className={cn(
              'size-9 shrink-0 rounded-full transition-all duration-200',
              hasDraft ? 'shadow-sm' : 'bg-muted text-text-tertiary',
            )}
            onClick={() => send()}
            disabled={!hasDraft}
            aria-label="Send message"
          >
            <ArrowUp className="size-4" />
          </Button>
        )}
      </div>

      {/* Shown until the first message is sent, and never on a phone, where there is no
          physical Enter key to explain and the row costs real reading height. */}
      {hintDismissed ? null : (
        <p className="text-text-tertiary hidden px-1 text-xs sm:block">
          <Kbd>Enter</Kbd> sends · <Kbd>Shift</Kbd> <Kbd>Enter</Kbd> for a new line
        </p>
      )}
    </div>
  )
}
