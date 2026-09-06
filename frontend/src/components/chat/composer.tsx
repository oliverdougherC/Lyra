'use client'

import Link from '@/router/link'
import { useCallback, useEffect, useLayoutEffect, useRef, type ReactNode } from 'react'
import { ArrowUp, Square } from 'lucide-react'

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
  /**
   * The agent's Stop is a round trip to the server's /stop: while it is in flight the
   * turn is being stopped but the server has not yet confirmed it, so the affordance
   * says "Stopping…" instead of claiming the turn is stopped. Clicking again is a no-op
   * (the pane guards the in-flight stop), so the button stays enabled and honest.
   */
  stopping?: boolean
  /** Non-null disables the composer and explains why. */
  disabledReason: string | null
  /** Temporarily prevent sending while conversation history is unavailable. */
  blocked?: boolean
  /**
   * The active source context, rendered on the small mark line beneath the input. Omitted
   * for a composer that has no material to scope (the writer reads the class, not a pick).
   */
  sourceControl?: ReactNode
  /**
   * The attached local workspace, rendered beside the source context: one glance answers
   * "what does Lyra have on hand for this task". Both marks are compact by contract -
   * the input line is the composer, and these keep to the small line beneath it.
   */
  workspaceControl?: ReactNode
  /** Take focus on mount, for a composer the reader has just opened deliberately. */
  autoFocus?: boolean
}

export function Composer({
  value,
  onChange,
  onSend,
  onStop,
  streaming,
  stopping = false,
  disabledReason,
  blocked = false,
  sourceControl,
  workspaceControl,
  autoFocus = false,
}: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  // Set on a keyboard send while the textarea owns focus: `disabled={streaming || blocked}` makes
  // the browser blur the control, and nothing else would give focus back when the turn
  // settles -- leaving a keyboard-only student stranded after every Enter-send.
  const restoreFocusAfterStream = useRef(false)
  const [hintDismissed, setHintDismissed] = useLocalStorageState(HINT_KEY, false, parseDismissed)

  // `preventScroll` because the caller places this composer itself. Letting the browser
  // scroll to it would fight that and land somewhere neither of them chose.
  useEffect(() => {
    if (autoFocus) textareaRef.current?.focus({ preventScroll: true })
  }, [autoFocus])

  // When the turn and its transcript refresh finish, restore a keyboard send's focus --
  // unless the student deliberately focused another interactive control meanwhile
  // (then stealing focus back would be worse than the original loss).
  useEffect(() => {
    if (streaming || blocked || disabledReason) return
    if (!restoreFocusAfterStream.current) return
    const node = textareaRef.current
    if (!node || node.disabled) return
    restoreFocusAfterStream.current = false
    const active = document.activeElement
    if (active !== null && active !== document.body && active !== node) return
    node.focus({ preventScroll: true })
  }, [streaming, blocked, disabledReason])

  // Auto-grow up to three rows, then let the textarea scroll internally.
  useLayoutEffect(() => {
    const node = textareaRef.current
    if (!node) return
    node.style.height = 'auto'
    node.style.height = `${Math.min(node.scrollHeight, MAX_ROWS * LINE_HEIGHT_PX + 16)}px`
  }, [value])

  const send = useCallback(
    (options?: { restoreFocusAfterStream?: boolean }) => {
      if (streaming || blocked || disabledReason || value.trim().length === 0) return
      // Each send owns the flag outright: a mouse send clears any leftover from an
      // earlier keyboard send whose turn never actually streamed.
      restoreFocusAfterStream.current = options?.restoreFocusAfterStream ?? false
      if (!hintDismissed) setHintDismissed(true)
      onSend()
    },
    [streaming, blocked, disabledReason, value, hintDismissed, setHintDismissed, onSend],
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
      {/* A sheet of writing paper laid on the canvas: the input line is the well's whole
          face, the send control riding it at the right. The lift comes from the surface
          being genuinely a different paper from the pane behind it, not from a heavier
          border. The context marks - what Lyra reads, the folder Lyra works in - keep to
          a small quiet line beneath the type, left to the margin, so the input owns the
          full width of the well in every window; each mark discloses its detail on click
          rather than exposing it at rest. There is no toolbar and no reserved row: the
          marks only exist when the composer has something to say about them. */}
      <div
        className={cn(
          'flex flex-col gap-1 rounded-2xl bg-card p-2.5',
          'border border-border/80 shadow-sm transition-[border-color,box-shadow] duration-200',
          'focus-within:border-accent-primary/60 focus-within:shadow-md',
        )}
      >
        <div className="flex items-center gap-2">
          <Textarea
            ref={textareaRef}
            rows={1}
            id="message-composer"
            name="message"
            value={value}
            disabled={streaming || blocked}
            placeholder="Ask about your material…"
            aria-label="Message Lyra"
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
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
              className={cn('size-9 shrink-0 rounded-full', stopping && 'animate-pulse')}
              onClick={onStop}
              aria-label={stopping ? 'Stopping…' : 'Stop generating'}
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
              disabled={!hasDraft || blocked}
              aria-label="Send message"
            >
              <ArrowUp className="size-4" />
            </Button>
          )}
        </div>
        {sourceControl || workspaceControl ? (
          <div data-source-control className="flex items-center gap-1.5 pl-2 text-text-tertiary">
            {sourceControl}
            {workspaceControl}
          </div>
        ) : null}
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
