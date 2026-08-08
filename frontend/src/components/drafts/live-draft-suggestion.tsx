'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { ListTree, PencilLine, Sparkles } from 'lucide-react'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api'
import {
  useFinalizeLiveDraftSuggestion,
  useUpdateLiveDraftSuggestionBlock,
} from '@/lib/hooks/use-drafts'
import { cn } from '@/lib/utils'
import type { LiveDraftSuggestion, LiveDraftSuggestionBlock, PendingEdit } from '@/types'

type LiveDraftSuggestionPanelProps = {
  draftId: number
  suggestion: LiveDraftSuggestion
  onFinalized: (edit: PendingEdit) => void
  onOpenPlan?: () => void
}

type EditBase = { content: string; revision: number }

const STAGES: Array<{ key: string; label: string }> = [
  { key: 'gathering', label: 'Gathering' },
  { key: 'outline', label: 'Outline' },
  { key: 'drafting', label: 'Drafting' },
  { key: 'transitions', label: 'Transitions' },
  { key: 'review', label: 'Review' },
  { key: 'finalize', label: 'Finalize' },
  { key: 'complete', label: 'Complete' },
]

const ACTIVE_STAGE: Record<string, string> = {
  gathering: 'gathering',
  outline: 'outline',
  drafting: 'drafting',
  transitions: 'transitions',
  review: 'review',
  finalize: 'finalize',
  complete: 'complete',
  done: 'complete',
}

function blockStatusLabel(status: LiveDraftSuggestionBlock['status']): string {
  switch (status) {
    case 'queued':
      return 'Queued'
    case 'drafting':
      return 'Drafting'
    case 'drafted':
      return 'Drafted'
    case 'revising':
      return 'Revising'
    case 'revised':
      return 'Revised'
    case 'failed':
      return 'Needs attention'
    default:
      return status.replaceAll('_', ' ')
  }
}

function blockStatusVariant(status: LiveDraftSuggestionBlock['status']) {
  if (status === 'failed') return 'destructive' as const
  if (status === 'revised' || status === 'drafted') return 'secondary' as const
  return 'outline' as const
}

function stageState(stage: string, active: string): 'done' | 'active' | 'upcoming' {
  const order = STAGES.map((entry) => entry.key)
  const activeIndex = Math.max(order.indexOf(active), 0)
  const stageIndex = order.indexOf(stage)
  if (stageIndex < activeIndex) return 'done'
  if (stageIndex === activeIndex) return 'active'
  return 'upcoming'
}

export function LiveDraftSuggestionPanel({
  draftId,
  suggestion,
  onFinalized,
  onOpenPlan,
}: LiveDraftSuggestionPanelProps) {
  const updateBlock = useUpdateLiveDraftSuggestionBlock(draftId)
  const finalize = useFinalizeLiveDraftSuggestion(draftId)
  const [drafts, setDrafts] = useState<Record<number, string>>({})
  const [dirtyBlockIds, setDirtyBlockIds] = useState<Set<number>>(() => new Set())
  const editBasesRef = useRef<Record<number, EditBase>>({})
  const dirtyBlockIdsRef = useRef(dirtyBlockIds)

  useEffect(() => {
    dirtyBlockIdsRef.current = dirtyBlockIds
  }, [dirtyBlockIds])

  useEffect(() => {
    setDrafts((current) => {
      const next = { ...current }
      for (const block of suggestion.blocks) {
        if (!(block.id in next) || !dirtyBlockIdsRef.current.has(block.id)) {
          next[block.id] = block.content
        }
      }
      for (const key of Object.keys(next)) {
        const blockId = Number(key)
        if (!suggestion.blocks.some((block) => block.id === blockId)) {
          delete next[blockId]
          delete editBasesRef.current[blockId]
        }
      }
      return next
    })
    setDirtyBlockIds((current) => {
      const next = new Set<number>()
      for (const block of suggestion.blocks) {
        if (current.has(block.id)) next.add(block.id)
      }
      return next
    })
  }, [suggestion])

  const activeStage = ACTIVE_STAGE[suggestion.stage] ?? 'gathering'
  const activeStageIndex = Math.max(
    STAGES.findIndex((stage) => stage.key === activeStage),
    0,
  )
  const completedBlocks = suggestion.blocks.filter((block) =>
    ['complete', 'drafted', 'revised'].includes(block.status),
  ).length
  const draftedWords = suggestion.blocks.reduce(
    (total, block) => total + (block.content.trim() ? block.content.trim().split(/\s+/).length : 0),
    0,
  )
  const canFinalize = suggestion.blocks.length > 0 && suggestion.status === 'ready'
  const savingBlockId =
    updateBlock.variables && updateBlock.isPending ? updateBlock.variables.blockId : null

  const dirtyBlocks = useMemo(
    () =>
      new Set<number>(
        suggestion.blocks.filter((block) => dirtyBlockIds.has(block.id)).map((block) => block.id),
      ),
    [dirtyBlockIds, suggestion.blocks],
  )

  function onConflict(error: unknown) {
    toast.error(error instanceof ApiError ? error.message : 'The live draft suggestion changed.')
  }

  function saveBlock(block: LiveDraftSuggestionBlock) {
    const content = drafts[block.id] ?? block.content
    const editBase = editBasesRef.current[block.id] ?? {
      content: block.content,
      revision: block.revision,
    }
    updateBlock.mutate(
      {
        blockId: block.id,
        content,
        expectedRevision: editBase.revision,
        baseContent: editBase.content,
      },
      {
        onSuccess: (updated) => {
          setDrafts((current) => ({ ...current, [updated.id]: updated.content }))
          setDirtyBlockIds((current) => {
            const next = new Set(current)
            next.delete(updated.id)
            return next
          })
          delete editBasesRef.current[updated.id]
          toast.success(`${block.heading ?? block.section_ref ?? 'Block'} saved.`)
        },
        onError: onConflict,
      },
    )
  }

  function finalizeSuggestion() {
    finalize.mutate(undefined, {
      onSuccess: (edit) => {
        toast.success('Live draft suggestion is ready for review.')
        onFinalized(edit)
      },
      onError: (error) => {
        toast.error(
          error instanceof ApiError
            ? error.message
            : 'Could not prepare the live draft suggestion for review.',
        )
      },
    })
  }

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <header className="flex flex-col gap-3">
        <div className="flex min-w-0 items-start justify-between gap-3">
          <div className="min-w-0 space-y-1">
            <div className="flex items-center gap-2">
              <Sparkles className="text-text-tertiary size-4 shrink-0" />
              <h2 className="text-text-primary text-sm font-medium">Live draft</h2>
            </div>
            <p className="text-text-secondary text-sm">
              The working document Lyra is assembling. Edit any paragraph while it develops; your
              real draft stays untouched until review.
            </p>
          </div>
          <Badge variant={suggestion.status === 'failed' ? 'destructive' : 'outline'}>
            {suggestion.status}
          </Badge>
        </div>
        <section
          className="border-border bg-muted/30 min-w-0 rounded-md border p-3"
          aria-label="Draft progress"
        >
          <div className="flex min-w-0 items-center justify-between gap-3 text-xs">
            <span className="text-text-tertiary shrink-0">
              Stage {activeStageIndex + 1} of {STAGES.length}
            </span>
            <span className="text-text-primary min-w-0 truncate font-medium">
              {STAGES[activeStageIndex]?.label}
            </span>
          </div>
          <ol className="mt-2 grid grid-cols-7 gap-1" aria-label="Drafting stages">
            {STAGES.map((stage) => {
              const state = stageState(stage.key, activeStage)
              return (
                <li
                  key={stage.key}
                  className={cn(
                    'bg-border h-1.5 min-w-0 rounded-full',
                    state === 'active' && 'bg-accent-primary',
                    state === 'done' && 'bg-accent-primary/50',
                  )}
                >
                  <span className="sr-only">
                    {stage.label}: {state}
                  </span>
                </li>
              )
            })}
          </ol>
          {suggestion.stage_detail ? (
            <p className="text-text-secondary mt-2 break-words text-sm">
              {suggestion.stage_detail}
            </p>
          ) : null}
        </section>
        <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-text-tertiary min-w-0 text-xs">
            {completedBlocks}/{suggestion.blocks.length || 0} paragraphs ·{' '}
            {draftedWords.toLocaleString()} words · Run {suggestion.run_id}
          </p>
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            {onOpenPlan ? (
              <Button variant="outline" size="sm" onClick={onOpenPlan}>
                <ListTree className="size-3.5" />
                View plan
              </Button>
            ) : null}
            <Button
              size="sm"
              className="min-w-0"
              disabled={!canFinalize || finalize.isPending}
              onClick={finalizeSuggestion}
            >
              <PencilLine className="size-3.5 shrink-0" />
              <span className="truncate">Review and merge</span>
            </Button>
          </div>
        </div>
      </header>

      <section
        className="border-border bg-card min-w-0 overflow-hidden rounded-md border"
        aria-label="Draft body"
      >
        <div className="border-border flex min-w-0 items-center justify-between gap-2 border-b px-3 py-2">
          <h3 className="text-text-primary text-sm font-medium">Draft body</h3>
          <span className="text-text-tertiary text-xs">Version {suggestion.version}</span>
        </div>
        <ul className="divide-border min-w-0 divide-y" aria-label="Live draft blocks">
          {suggestion.blocks.map((block, index) => {
            const title = block.heading ?? block.section_ref ?? `Block ${block.ordinal}`
            const value = drafts[block.id] ?? block.content
            const busy = savingBlockId === block.id
            const previous = suggestion.blocks[index - 1]
            const showSection = Boolean(block.heading && block.heading !== previous?.heading)
            return (
              <li key={block.id} className="min-w-0 p-3">
                <article className="min-w-0">
                  {showSection ? (
                    <p className="eyebrow text-text-tertiary mb-2 break-words">{block.heading}</p>
                  ) : null}
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <span className="text-text-primary text-sm font-medium">
                      Paragraph {block.ordinal}
                    </span>
                    <Badge variant={blockStatusVariant(block.status)}>
                      {blockStatusLabel(block.status)}
                    </Badge>
                    {block.target_words ? (
                      <span className="text-text-tertiary text-xs">
                        Target {block.target_words} words
                      </span>
                    ) : null}
                  </div>
                  {block.summary ? (
                    <details className="text-text-tertiary mt-2 text-xs">
                      <summary className="hover:text-text-secondary cursor-pointer select-none">
                        Plan note
                      </summary>
                      <p className="mt-1 break-words">{block.summary}</p>
                    </details>
                  ) : null}
                  <label className="sr-only" htmlFor={`live-block-${block.id}`}>
                    {title} draft block
                  </label>
                  <textarea
                    id={`live-block-${block.id}`}
                    aria-label={`${title} draft block`}
                    className="border-border bg-background text-foreground mt-3 min-h-32 w-full min-w-0 resize-y overflow-y-auto rounded-md border px-3 py-2 text-sm leading-6 outline-none [field-sizing:content] focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/30"
                    value={value}
                    onChange={(event) => {
                      const nextValue = event.target.value
                      if (!dirtyBlockIdsRef.current.has(block.id)) {
                        editBasesRef.current[block.id] = {
                          content: block.content,
                          revision: block.revision,
                        }
                      }
                      setDrafts((current) => ({ ...current, [block.id]: nextValue }))
                      setDirtyBlockIds((current) => {
                        const next = new Set(current)
                        if (nextValue === block.content) {
                          next.delete(block.id)
                          delete editBasesRef.current[block.id]
                        } else next.add(block.id)
                        return next
                      })
                    }}
                  />
                  <div className="mt-2 flex min-w-0 flex-wrap items-center justify-between gap-2">
                    <p className="text-text-tertiary text-xs">
                      Revision {block.revision}
                      {block.user_revision > 0 ? ` • Your edits ${block.user_revision}` : ''}
                    </p>
                    <Button
                      variant="outline"
                      size="sm"
                      aria-label={`Save ${title}`}
                      disabled={busy || !dirtyBlocks.has(block.id)}
                      onClick={() => saveBlock(block)}
                    >
                      {busy ? 'Saving…' : 'Save'}
                    </Button>
                  </div>
                </article>
              </li>
            )
          })}
        </ul>
      </section>
    </div>
  )
}
