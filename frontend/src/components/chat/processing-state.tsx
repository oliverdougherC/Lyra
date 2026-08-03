'use client'

import { Check } from 'lucide-react'

import { Spinner } from '@/components/ui/spinner'
import { cn } from '@/lib/utils'

export type ProcessingStage = 'prompt_processing' | 'reviewing_documents' | 'composing_answer'

const STAGES: readonly {
  id: ProcessingStage
  label: string
  detail: string
}[] = [
  {
    id: 'prompt_processing',
    label: 'Processing prompt',
    detail: 'Preparing your question and conversation context.',
  },
  {
    id: 'reviewing_documents',
    label: 'Reviewing documents',
    detail: 'Selecting relevant passages from your course material.',
  },
  {
    id: 'composing_answer',
    label: 'Writing explanation',
    detail: 'Composing a response grounded in the selected material.',
  },
]

export function isProcessingStage(value: unknown): value is ProcessingStage {
  return STAGES.some((stage) => stage.id === value)
}

type ProcessingStateProps = {
  stage: ProcessingStage
}

export function ProcessingState({ stage }: ProcessingStateProps) {
  const currentIndex = STAGES.findIndex((item) => item.id === stage)
  const current = STAGES[currentIndex]

  return (
    <div className="space-y-3 rounded-md border border-border bg-card p-3" aria-busy="true">
      <div aria-live="polite" className="flex items-start gap-2.5">
        <Spinner aria-hidden className="mt-0.5 text-accent-primary" />
        <div className="min-w-0">
          <p className="text-sm font-medium text-text-primary">{current.label}</p>
          <p className="mt-0.5 text-xs leading-5 text-text-secondary">{current.detail}</p>
        </div>
      </div>

      <ol aria-hidden="true" className="space-y-1.5 border-l border-border pl-3">
        {STAGES.map((item, index) => {
          const completed = index < currentIndex
          const active = index === currentIndex

          return (
            <li
              key={item.id}
              className={cn(
                'flex items-center gap-2 text-xs',
                completed || active ? 'text-text-secondary' : 'text-text-tertiary',
              )}
            >
              {completed ? (
                <Check aria-hidden className="size-3.5 shrink-0 text-success-text" />
              ) : (
                <span
                  aria-hidden
                  className={cn(
                    'size-1.5 shrink-0 rounded-full',
                    active ? 'bg-accent-primary' : 'bg-border-strong/70',
                  )}
                />
              )}
              <span>{item.label}</span>
            </li>
          )
        })}
      </ol>
    </div>
  )
}
