'use client'

import { useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { toast } from 'sonner'

import { ChatPane } from '@/components/chat/chat-pane'
import { StreamingMarkdown } from '@/components/chat/streaming-markdown'
import { Sheet, SheetContent, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { api, ApiError } from '@/lib/api'
import { chatKeys, useCreateSession } from '@/lib/hooks/use-chat'
import type { SolutionPart } from '@/types'

type StepGuidePanelProps = {
  classId: number
  className: string
  step: SolutionPart | null
  onClose: () => void
}

/**
 * A real conversation about one step, opened from the step's action row.
 *
 * It reuses the Phase 1 chat stack entirely: the same composer, the same streaming
 * markdown, the same reasoning trace, the same Guide and Show toggle opening on Guide. It
 * is an ordinary session that happens to be anchored, and it appears in the sidebar under
 * Conversations like any other. This is what makes the solver and the conversation one
 * product at two altitudes rather than two features.
 */
export function StepGuidePanel({ classId, className, step, onClose }: StepGuidePanelProps) {
  const queryClient = useQueryClient()
  const createSession = useCreateSession(classId)
  const { mutate, reset, data } = createSession

  // One session per step opening. Reusing the previous one would anchor the new question
  // to the step the student was reading a moment ago. The session id is read off the
  // mutation rather than copied into state, so there is no second copy to fall out of step.
  useEffect(() => {
    if (step === null) {
      reset()
      return
    }
    mutate(step.id, {
      onError: (error) =>
        toast.error(
          error instanceof ApiError ? error.message : 'Could not open a conversation here.',
        ),
    })
  }, [step, mutate, reset])

  const sessionId = data?.id ?? null

  /**
   * Closing without having asked anything takes the empty conversation with it.
   *
   * The session has to exist before the panel can hold a conversation, so opening the
   * panel creates one. Without this, changing your mind would leave an untitled chat in
   * the sidebar every time, and a student who browsed six steps would find six of them.
   */
  const handleClose = async () => {
    onClose()
    if (sessionId === null) return
    try {
      const messages = await api.listMessages(sessionId)
      if (messages.length > 0) return
      await api.deleteSession(sessionId)
      queryClient.invalidateQueries({ queryKey: chatKeys.sessions(classId) })
    } catch {
      // An empty conversation left behind is untidy, not broken. Nothing worth telling
      // the student about while they are closing a panel.
    }
  }

  return (
    <Sheet open={step !== null} onOpenChange={(open) => (open ? null : void handleClose())}>
      <SheetContent side="right" className="w-full gap-0 p-0 sm:max-w-[480px]">
        <SheetHeader className="border-border border-b">
          <SheetTitle>Ask about this step</SheetTitle>
          {/* The step is pinned at the top of the panel, quoted and quiet, so the subject
              of the conversation is never ambiguous. */}
          {step ? (
            <blockquote className="border-border text-text-secondary max-h-40 overflow-y-auto border-l-2 pl-3 text-sm">
              {step.label ? <span className="block font-medium">{step.label}</span> : null}
              {/* Rendered rather than quoted raw: the student is looking at this same step
                  typeset in the pane behind the panel, and a wall of LaTeX source here
                  would not be recognisable as the thing they clicked. */}
              <div className="assistant-content text-sm">
                <StreamingMarkdown content={step.content} />
              </div>
            </blockquote>
          ) : null}
        </SheetHeader>

        <div className="min-h-0 flex-1">
          {sessionId === null ? (
            <div className="flex flex-col gap-3 p-4" aria-busy="true">
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-4 w-1/2" />
            </div>
          ) : (
            <ChatPane
              classId={classId}
              className={className}
              selectedDocumentId={null}
              onClearSelectedDocument={() => undefined}
              sessionId={sessionId}
              emptyState={
                <p className="text-text-tertiary px-2 py-8 text-center text-sm">
                  Ask anything about this step. Lyra opens in Guide, so it will work through it with
                  you rather than restating it.
                </p>
              }
            />
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
