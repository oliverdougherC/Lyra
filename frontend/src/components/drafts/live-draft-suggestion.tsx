'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { PencilLine, Sparkles } from 'lucide-react'
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
    toast.error(
      error instanceof ApiError ? error.message : 'The live draft suggestion changed.',
    )
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
    <div className="flex flex-col gap-4">
      <header className="flex flex-col gap-3">
        <div className="flex items-start justify-between gap-3">
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <Sparkles className="text-text-tertiary size-4" />
              <h2 className="text-text-primary text-sm font-medium">Live draft suggestion</h2>
            </div>
            <p className="text-text-secondary text-sm">
              Drafted separately from your document. Keep editing the real draft while Lyra builds
              this proposal block by block.
            </p>
          </div>
          <Badge variant={suggestion.status === 'failed' ? 'destructive' : 'outline'}>
            {suggestion.status}
          </Badge>
        </div>
        <ol className="grid grid-cols-2 gap-2 xl:grid-cols-7" aria-label="Drafting stages">
          {STAGES.map((stage) => {
            const state = stageState(stage.key, activeStage)
            return (
              <li
                key={stage.key}
                className={cn(
                  'border-border rounded-md border px-3 py-2 text-sm',
                  state === 'active' && 'border-border-strong bg-muted',
                  state === 'done' && 'bg-accent-surface/40',
                )}
              >
                <span className="text-text-primary block font-medium">{stage.label}</span>
              </li>
            )
          })}
        </ol>
        {suggestion.stage_detail ? (
          <p className="text-text-secondary text-sm">{suggestion.stage_detail}</p>
        ) : null}
        <div className="flex items-center justify-between gap-2">
          <p className="text-text-tertiary text-xs">
            Version {suggestion.version} • Run {suggestion.run_id}
          </p>
          <Button size="sm" disabled={!canFinalize || finalize.isPending} onClick={finalizeSuggestion}>
            <PencilLine className="size-3.5" />
            Final review and merge
          </Button>
        </div>
      </header>

      <ul className="flex flex-col gap-3" aria-label="Live draft blocks">
        {suggestion.blocks.map((block) => {
          const title = block.heading ?? block.section_ref ?? `Block ${block.ordinal}`
          const value = drafts[block.id] ?? block.content
          const busy = savingBlockId === block.id
          return (
            <li key={block.id}>
              <article className="border-border bg-card rounded-md border p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-text-primary text-sm font-medium">{title}</h3>
                  <Badge variant={blockStatusVariant(block.status)}>{blockStatusLabel(block.status)}</Badge>
                  {block.target_words ? (
                    <Badge variant="outline">Target {block.target_words} words</Badge>
                  ) : null}
                  {block.section_ref ? <Badge variant="outline">{block.section_ref}</Badge> : null}
                </div>
                {block.summary ? (
                  <p className="text-text-secondary mt-2 text-sm">{block.summary}</p>
                ) : null}
                <label className="sr-only" htmlFor={`live-block-${block.id}`}>
                  {title} draft block
                </label>
                <textarea
                  id={`live-block-${block.id}`}
                  aria-label={`${title} draft block`}
                  className="border-border bg-background text-foreground mt-3 min-h-36 w-full rounded-md border px-3 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/30"
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
                <div className="mt-3 flex items-center justify-between gap-2">
                  <p className="text-text-tertiary text-xs">
                    Revision {block.revision}
                    {block.user_revision > 0 ? ` • Your edits ${block.user_revision}` : ''}
                  </p>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={busy || !dirtyBlocks.has(block.id)}
                    onClick={() => saveBlock(block)}
                  >
                    {busy ? 'Saving…' : `Save ${title}`}
                  </Button>
                </div>
              </article>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
