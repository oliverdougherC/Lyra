'use client'

import { Check, Copy, History, MessageCircleQuestion, Pencil } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import { StreamingMarkdown } from '@/components/chat/streaming-markdown'
import { ProvenanceChip } from '@/components/solutions/provenance-chip'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { ApiError } from '@/lib/api'
import { useUpdatePart } from '@/lib/hooks/use-solutions'
import { cn } from '@/lib/utils'
import type { SolutionPart } from '@/types'

type SolutionStepProps = {
  step: SolutionPart
  /** Position among the problem's steps, 1-based. Absent for the final answer. */
  index?: number
  onAsk: (step: SolutionPart) => void
  onHistory: (step: SolutionPart) => void
  /** Dimmed while the problem this belongs to is being solved again. */
  dimmed?: boolean
}

/**
 * One step of a solution, on the Phase 1 assistant reading surface.
 *
 * The action row is hidden until the row is hovered or something in it takes focus,
 * matching the message action row. Focus counts as well as hover, because every action in
 * this pane has to be reachable without a pointer.
 */
export function SolutionStep({ step, index, onAsk, onHistory, dimmed = false }: SolutionStepProps) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(step.content)
  const [copied, setCopied] = useState(false)
  const save = useUpdatePart(step.artifact_id)

  const isAnswer = step.kind === 'answer'

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(step.content)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      toast.error('Could not copy that step.')
    }
  }

  const handleSave = () => {
    const cleaned = draft.trim()
    if (!cleaned) {
      toast.error('A step cannot be empty.')
      return
    }
    save.mutate(
      { partId: step.id, content: cleaned },
      {
        onSuccess: () => setEditing(false),
        onError: (error) =>
          toast.error(error instanceof ApiError ? error.message : 'Could not save that edit.'),
      },
    )
  }

  const startEditing = () => {
    setDraft(step.content)
    setEditing(true)
  }

  return (
    <div
      className={cn(
        'group/step flex flex-col gap-2 transition-opacity duration-200',
        dimmed && 'opacity-60',
      )}
    >
      <div className="flex items-baseline gap-2">
        {index !== undefined ? (
          <span className="text-text-tertiary shrink-0 text-xs tabular-nums">Step {index}</span>
        ) : null}
        <h4
          className={cn(
            'text-text-primary min-w-0 text-sm font-medium',
            isAnswer && 'text-accent-surface-foreground',
          )}
        >
          {step.label ?? (isAnswer ? 'Answer' : null)}
        </h4>
        {step.origin === 'user_corrected' ? (
          <span className="text-text-tertiary shrink-0 text-xs">Your edit</span>
        ) : null}
      </div>

      {editing ? (
        <div className="flex flex-col gap-2">
          <Textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            // Escape cancels; Enter must insert a newline, because a step is long enough
            // that committing on Enter would lose work.
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                event.preventDefault()
                setEditing(false)
              }
            }}
            rows={Math.min(16, draft.split('\n').length + 2)}
            className="font-mono text-sm"
            aria-label={`Edit ${step.label ?? 'this step'}`}
            autoFocus
          />
          <div className="flex gap-2">
            <Button size="sm" onClick={handleSave} disabled={save.isPending}>
              {save.isPending ? 'Saving' : 'Save'}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <div
          className={cn('assistant-content', isAnswer && 'border-accent-primary border-l-2 pl-3')}
        >
          <StreamingMarkdown content={step.content} />
        </div>
      )}

      <ProvenanceChip entries={step.provenance} />

      {editing ? null : (
        <div className="flex gap-0.5 opacity-0 transition-opacity group-focus-within/step:opacity-100 group-hover/step:opacity-100 print:hidden">
          <StepAction label="Ask about this step" onClick={() => onAsk(step)}>
            <MessageCircleQuestion className="size-3.5" />
          </StepAction>
          <StepAction label="Edit" onClick={startEditing}>
            <Pencil className="size-3.5" />
          </StepAction>
          {/* Beside Edit, because history is what makes editing safe: it is the way back
              from a change the student decides against. */}
          <StepAction label="History" onClick={() => onHistory(step)}>
            <History className="size-3.5" />
          </StepAction>
          <StepAction label={copied ? 'Copied' : 'Copy'} onClick={handleCopy}>
            {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
          </StepAction>
        </div>
      )}
    </div>
  )
}

function StepAction({
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
          size="sm"
          className="text-text-tertiary hover:text-text-primary size-7 p-0"
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
