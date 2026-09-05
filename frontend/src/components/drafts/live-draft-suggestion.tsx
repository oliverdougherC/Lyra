'use client'

import { useEffect, useRef, useState } from 'react'
import { ListTree, PencilLine, Sparkles } from 'lucide-react'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { api, ApiError } from '@/lib/api'
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

type EditBase = { content: string; revision: number; userRevision: number }

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
  const draftsRef = useRef(drafts)
  const dirtyBlockIdsRef = useRef(dirtyBlockIds)
  const savesRef = useRef(new Map<number, Promise<void>>())
  const uncertainSavesRef = useRef<
    Record<number, { content: string; revision: number; userRevision: number }>
  >({})
  const [savingBlockIds, setSavingBlockIds] = useState<Set<number>>(() => new Set())
  const [preparing, setPreparing] = useState(false)
  const preparingRef = useRef(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const next = { ...draftsRef.current }
    for (const block of suggestion.blocks) {
      const saved = editBasesRef.current[block.id]
      if (
        !dirtyBlockIdsRef.current.has(block.id) &&
        !savesRef.current.has(block.id) &&
        (!saved || block.revision >= saved.revision)
      ) {
        next[block.id] = block.content
        editBasesRef.current[block.id] = {
          content: block.content,
          revision: block.revision,
          userRevision: block.user_revision,
        }
      }
    }
    draftsRef.current = next
    setDrafts(next)
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

  function reportError(error: unknown) {
    const message =
      error instanceof ApiError
        ? error.message
        : 'Could not save and prepare this draft. Your edits are still here; try again.'
    setError(message)
    toast.error(message)
  }

  function acknowledgeSave(updated: LiveDraftSuggestionBlock, submitted: string) {
    const current = draftsRef.current[updated.id] ?? submitted
    // The server preserves any model text streamed since our base as an appended suffix.
    // Keep that suffix alongside input typed after submission instead of silently deleting it
    // on the next save with the newly acknowledged revision.
    const suffix = updated.content.slice(submitted.length)
    const nextValue = current === submitted ? updated.content : current + suffix
    draftsRef.current = { ...draftsRef.current, [updated.id]: nextValue }
    setDrafts(draftsRef.current)
    editBasesRef.current[updated.id] = {
      content: updated.content,
      revision: updated.revision,
      userRevision: updated.user_revision,
    }
    const nextDirty = new Set(dirtyBlockIdsRef.current)
    if (nextValue === updated.content) nextDirty.delete(updated.id)
    else nextDirty.add(updated.id)
    dirtyBlockIdsRef.current = nextDirty
    setDirtyBlockIds(nextDirty)
  }

  function saveBlock(block: LiveDraftSuggestionBlock): Promise<void> {
    const pending = savesRef.current.get(block.id)
    if (pending) return pending
    setSavingBlockIds((current) => new Set(current).add(block.id))
    const request = (async () => {
      const uncertain = uncertainSavesRef.current[block.id]
      if (uncertain) {
        // A lost response does not prove a failed write. Read back before retrying, so an
        // already-landed replacement becomes the base instead of causing permanent conflicts.
        const live = await api.getLiveDraftSuggestion(draftId)
        const remote = live?.blocks.find((entry) => entry.id === block.id)
        if (
          remote &&
          remote.revision > uncertain.revision &&
          remote.user_revision > uncertain.userRevision &&
          remote.content.startsWith(uncertain.content)
        ) {
          acknowledgeSave(remote, uncertain.content)
        }
        delete uncertainSavesRef.current[block.id]
        if (!dirtyBlockIdsRef.current.has(block.id)) return
      }
      const content = draftsRef.current[block.id] ?? block.content
      const editBase = editBasesRef.current[block.id] ?? {
        content: block.content,
        revision: block.revision,
        userRevision: block.user_revision,
      }
      let updated: LiveDraftSuggestionBlock
      try {
        updated = await updateBlock.mutateAsync({
          blockId: block.id,
          content,
          expectedRevision: editBase.revision,
          baseContent: editBase.content,
        })
      } catch (error) {
        uncertainSavesRef.current[block.id] = {
          content,
          revision: editBase.revision,
          userRevision: editBase.userRevision,
        }
        throw error
      }
      acknowledgeSave(updated, content)
    })().finally(() => {
      savesRef.current.delete(block.id)
      setSavingBlockIds((current) => {
        const next = new Set(current)
        next.delete(block.id)
        return next
      })
    })
    savesRef.current.set(block.id, request)
    return request
  }

  async function finalizeSuggestion() {
    if (preparingRef.current || !canFinalize) return
    preparingRef.current = true
    setPreparing(true)
    setError(null)
    try {
      // Finish earlier saves first: they may leave newer typing still unsaved.
      await Promise.all([...savesRef.current.values()])
      for (const block of suggestion.blocks) {
        if (dirtyBlockIdsRef.current.has(block.id)) await saveBlock(block)
      }
      const edit = await finalize.mutateAsync()
      toast.success('Live draft suggestion is ready for review.')
      onFinalized(edit)
    } catch (error) {
      reportError(error)
    } finally {
      preparingRef.current = false
      setPreparing(false)
    }
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
            {draftedWords.toLocaleString()} words
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
              disabled={!canFinalize || preparing}
              onClick={() => void finalizeSuggestion()}
            >
              <PencilLine className="size-3.5 shrink-0" />
              <span className="truncate">
                {preparing ? 'Preparing review…' : 'Review and merge'}
              </span>
            </Button>
          </div>
        </div>
      </header>

      {error ? (
        <p role="alert" className="text-danger-text text-sm">
          {error}
        </p>
      ) : null}
      <section
        className="border-border bg-card min-w-0 overflow-hidden rounded-md border"
        aria-label="Draft body"
      >
        <div className="border-border flex min-w-0 items-center justify-between gap-2 border-b px-3 py-2">
          <h3 className="text-text-primary text-sm font-medium">Draft body</h3>
          <span className="text-text-tertiary text-xs" role="status">
            {preparing
              ? 'Saving edits and preparing review…'
              : dirtyBlockIds.size > 0
                ? 'Unsaved edits'
                : 'Saved'}
          </span>
        </div>
        <ul className="divide-border min-w-0 divide-y" aria-label="Live draft blocks">
          {suggestion.blocks.map((block, index) => {
            const title = block.heading ?? block.section_ref ?? `Block ${block.ordinal}`
            const value = drafts[block.id] ?? block.content
            const busy = savingBlockIds.has(block.id)
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
                    disabled={preparing}
                    value={value}
                    onChange={(event) => {
                      const nextValue = event.target.value
                      const base = editBasesRef.current[block.id] ?? {
                        content: block.content,
                        revision: block.revision,
                        userRevision: block.user_revision,
                      }
                      editBasesRef.current[block.id] = base
                      draftsRef.current = { ...draftsRef.current, [block.id]: nextValue }
                      setDrafts(draftsRef.current)
                      const next = new Set(dirtyBlockIdsRef.current)
                      if (
                        nextValue === base.content &&
                        !savesRef.current.has(block.id) &&
                        !uncertainSavesRef.current[block.id]
                      )
                        next.delete(block.id)
                      else next.add(block.id)
                      dirtyBlockIdsRef.current = next
                      setDirtyBlockIds(next)
                    }}
                  />
                  <div className="mt-2 flex min-w-0 flex-wrap items-center justify-between gap-2">
                    <p className="text-text-tertiary text-xs">
                      {busy ? 'Saving…' : dirtyBlockIds.has(block.id) ? 'Unsaved edits' : 'Saved'}
                    </p>
                    <Button
                      variant="outline"
                      size="sm"
                      aria-label={`Save ${title}`}
                      disabled={busy || preparing || !dirtyBlockIds.has(block.id)}
                      onClick={() => {
                        setError(null)
                        void saveBlock(block)
                          .then(() => toast.success(`${title} saved.`))
                          .catch(reportError)
                      }}
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
