'use client'

import { X } from 'lucide-react'
import { useLayoutEffect, useRef, useState } from 'react'

import { ChatPane } from '@/components/chat/chat-pane'
import { Button } from '@/components/ui/button'
import type { SolutionPart } from '@/types'

/** A little air under the composer, so it does not sit flush on the fold. */
const BREATHING_PX = 12

type StepThreadProps = {
  classId: number
  className: string
  /** The step this conversation is about. Pinned into every turn by the backend. */
  step: SolutionPart
  /** The scrolling ancestor, so a streaming answer can be followed where it sits. */
  scrollViewportRef: React.RefObject<HTMLDivElement | null>
  onClose: () => void
}

/**
 * A conversation about one step, opened underneath that step rather than over it.
 *
 * This used to be a 480px sheet with the workspace blurred behind it, which meant the one
 * thing a student asking "how did we get here" needs to see, the step before this one, was
 * the thing the panel covered. Inline, the whole solution stays where it was: the question
 * is asked in the margin of the document, not in a room next to it.
 *
 * It reuses the Phase 1 chat stack entirely: the same composer, the same streaming
 * markdown, the same reasoning trace, the same Guide and Show toggle opening on Guide, and
 * it appears in the sidebar under Conversations like any other. That is what makes the
 * solver and the conversation one product at two altitudes rather than two features.
 *
 * Nothing is created on the server until the student sends something. Reading a solution
 * with the thread open on six different steps used to leave six untitled conversations
 * behind it.
 */
export function StepThread({
  classId,
  className,
  step,
  scrollViewportRef,
  onClose,
}: StepThreadProps) {
  const [sessionId, setSessionId] = useState<number | null>(null)
  const sectionRef = useRef<HTMLElement>(null)

  /**
   * Bring the composer on screen, once, as the thread opens.
   *
   * The thread lands below the step that was clicked, which is usually mid-pane, so
   * without this the reader has to scroll down to find the box they just asked for. Only
   * ever downward: if the whole thread is already visible there is nothing to correct, and
   * moving the pane anyway would take the step away from under them.
   */
  useLayoutEffect(() => {
    const node = sectionRef.current
    const viewport = scrollViewportRef.current
    if (!node || !viewport) return
    const overshoot =
      node.getBoundingClientRect().bottom - viewport.getBoundingClientRect().bottom + BREATHING_PX
    if (overshoot > 0) viewport.scrollTo({ top: viewport.scrollTop + overshoot })
  }, [scrollViewportRef])

  return (
    <section
      ref={sectionRef}
      aria-label={`Conversation about ${step.label ?? 'this step'}`}
      // The rail ties the thread to the step above it, in the accent that marks Lyra's own
      // voice everywhere else in the pane.
      className="border-accent-primary/40 mt-1 border-l-2 pt-1 pl-4 print:hidden"
    >
      <div className="mb-3 flex items-center gap-2">
        {/* Sentence case, not the pane's uppercase label style. "ASKING ABOUT ANSWER"
            shouts, and this is the quietest thing on the screen. */}
        <h4 className="text-text-tertiary text-xs font-medium">
          Asking about {step.label ? `"${step.label}"` : 'this step'}
        </h4>
        <Button
          variant="ghost"
          size="sm"
          className="text-text-tertiary hover:text-text-primary ml-auto size-7 p-0"
          onClick={onClose}
          aria-label="Close this conversation"
        >
          <X className="size-3.5" />
        </Button>
      </div>

      <ChatPane
        classId={classId}
        className={className}
        selectedDocumentId={null}
        onClearSelectedDocument={() => undefined}
        sessionId={sessionId}
        draft={sessionId === null}
        anchorPartId={step.id}
        layout="inline"
        scrollViewportRef={scrollViewportRef}
        onSessionIdChange={setSessionId}
        // Nothing at all before the first question. The header names the step, the composer
        // is focused, and a paragraph explaining that you may type in the box you are
        // already typing in costs a third of the thread's height to say nothing.
        emptyState={null}
      />
    </section>
  )
}
