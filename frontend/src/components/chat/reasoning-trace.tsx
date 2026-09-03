'use client'

import { useLayoutEffect, useRef, useState } from 'react'
import { ChevronRight } from 'lucide-react'

import { StreamingMarkdown } from '@/components/chat/streaming-markdown'
import { ThinkingIndicator } from '@/components/chat/thinking-indicator'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { cn } from '@/lib/utils'

/**
 * A reasoning model's thought, shown live while the turn it belongs to is still moving.
 *
 * Always closed until the reader opens it. A thought is the model's working, not the reply:
 * unfolding it unasked pushes the answer down the page and makes the reader watch a draft
 * they did not ask to read. The header still reports that thinking is happening, which is
 * what the wait actually needs; the thought behind it is one click away, live while the
 * model is still writing it.
 *
 * A trace opened mid-turn returns to closed when the turn lands, because the streaming row
 * is replaced by the stored message and this unmounts with it. The stored message keeps the
 * thought under its own collapsed `Details` disclosure (see `TurnDetails`), which is the
 * right end state: once the answer is there, the answer is what the reader came for.
 *
 * A model that does not think never renders this at all.
 */

/**
 * How tall the live thought is allowed to grow before it scrolls itself instead, with the
 * top edge faded out so text sliding past the cut reads as a window onto a longer thought
 * rather than as a line chopped in half.
 */
const LIVE_WINDOW = 'max-h-[9.5rem] [mask-image:linear-gradient(to_bottom,transparent,black_2rem)]'

type ReasoningTraceProps = {
  text: string
  /** True while the thought itself is still arriving. */
  streaming?: boolean
  /** When the turn started, for the live counter. */
  startedAt?: number | null
}

export function ReasoningTrace({ text, streaming = false, startedAt = null }: ReasoningTraceProps) {
  const [open, setOpen] = useState(false)
  const bodyRef = useRef<HTMLDivElement>(null)
  // The reader may scroll back through a long thought while it is still being written;
  // following the tail stops the moment they do, and resumes when they return to it.
  const followingRef = useRef(true)

  useLayoutEffect(() => {
    const node = bodyRef.current
    if (!node || !streaming || !followingRef.current) return
    node.scrollTop = node.scrollHeight
  }, [text, streaming, open])

  if (!text.trim()) return null

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="mb-3">
      {/* One trigger for both live states. A live thought has to be reachable, or the
          reader is told the model is thinking and given no way to look. The settled
          record lives with the stored message, not here. */}
      {/* `h-7` matches the mark beside it and a line of the answer's prose, so the first row
          of a reply is one consistent height whether it leads with a thought, a wait, or the
          answer itself, and the mark never shifts as a turn moves between them. */}
      <CollapsibleTrigger
        className={cn(
          'group/trace -mx-1 flex h-7 items-center gap-1.5 rounded-md px-1',
          'transition-colors focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none',
          streaming ? 'text-text-secondary' : 'text-text-tertiary hover:text-text-secondary',
        )}
      >
        <ChevronRight
          aria-hidden
          className="size-3 shrink-0 transition-transform duration-200 group-data-[state=open]/trace:rotate-90"
        />
        {/* Live, the counter is the wait made visible. Settled-but-still-streaming, the
            thought is done and the answer is moving, so a static word is the honest
            label: the elapsed duration is nominal, and it no longer travels with the
            stored message. */}
        {streaming ? (
          <ThinkingIndicator label="Thinking" startedAt={startedAt} />
        ) : (
          <span className="text-xs">Thought</span>
        )}
      </CollapsibleTrigger>

      <CollapsibleContent
        className={cn(
          'overflow-hidden',
          'data-[state=closed]:animate-collapsible-up data-[state=open]:animate-collapsible-down',
        )}
      >
        <div
          ref={bodyRef}
          onScroll={(event) => {
            const node = event.currentTarget
            followingRef.current = node.scrollHeight - node.scrollTop - node.clientHeight <= 24
          }}
          className={cn(
            'reasoning-body scrollbar-none text-text-secondary mt-2 overflow-y-auto border-l border-border pl-3',
            'font-ai-response text-[0.9375rem] leading-6',
            streaming ? LIVE_WINDOW : 'max-h-[22rem]',
          )}
        >
          {/* Markdown only once the thought has settled. Re-parsing several thousand
              characters on every delta is real work, and it buys nothing on text that is
              scrolling past faster than anyone reads it. */}
          {streaming ? (
            <div className="whitespace-pre-wrap">{text}</div>
          ) : (
            <StreamingMarkdown content={text} />
          )}
        </div>
      </CollapsibleContent>
    </Collapsible>
  )
}
