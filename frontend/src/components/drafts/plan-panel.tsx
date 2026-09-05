'use client'

import { useQueryClient } from '@tanstack/react-query'
import { Play, Save } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '@/components/ui/spinner'
import { Textarea } from '@/components/ui/textarea'
import { draftKeys, useDraftPlan, useUpdateDraftPlan } from '@/lib/hooks/use-drafts'
import type { DraftPlan, DraftPlanSection } from '@/types'

type EditablePlan = Pick<DraftPlan, 'brief_analysis' | 'thesis' | 'argument_map' | 'sections'>

function copyPlan(plan: DraftPlan): EditablePlan {
  return {
    brief_analysis: plan.brief_analysis,
    thesis: plan.thesis,
    argument_map: plan.argument_map,
    sections: plan.sections.map((section) => ({
      ...section,
      evidence: [...section.evidence],
      sources: [...section.sources],
    })),
  }
}

/** The writer's persistent intent, visible and editable between every model call. */
export function PlanPanel({
  draftId,
  running = false,
  onRun,
}: {
  draftId: number
  running?: boolean
  onRun?: () => Promise<void>
}) {
  const queryClient = useQueryClient()
  const query = useDraftPlan(draftId)
  const update = useUpdateDraftPlan(draftId)
  const plan = query.data ?? null
  const [draft, setDraft] = useState<EditablePlan | null>(null)
  const [seenVersion, setSeenVersion] = useState<number | null>(null)
  const [baseline, setBaseline] = useState<EditablePlan | null>(null)
  const [newerPlan, setNewerPlan] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const dirty = draft !== null && JSON.stringify(draft) !== JSON.stringify(baseline)

  if (plan && plan.version !== seenVersion) {
    const next = copyPlan(plan)
    setSeenVersion(plan.version)
    if (!dirty) {
      setDraft(next)
      setBaseline(next)
    } else {
      setNewerPlan(true)
    }
  }

  if (query.isPending) {
    return (
      <div className="flex flex-col gap-3" aria-busy="true">
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    )
  }

  const loadError = query.isError ? (
    <div className="space-y-3" role="alert">
      <p className="text-danger-text text-sm">The saved plan could not be loaded.</p>
      <Button variant="outline" size="sm" onClick={() => void query.refetch()}>
        Retry
      </Button>
    </div>
  ) : null

  if (query.isError && (!plan || !draft)) return loadError

  if (!plan || !draft) {
    return (
      <div className="space-y-2">
        <p className="text-text-primary text-sm font-medium">No plan yet</p>
        <p className="text-text-tertiary text-sm">
          Start a document pass and Lyra will save its thesis, argument map, and section jobs here
          before it drafts.
        </p>
      </div>
    )
  }

  function changeSection(id: number, patch: Partial<DraftPlanSection>) {
    setDraft((current) =>
      current
        ? {
            ...current,
            sections: current.sections.map((section) =>
              section.id === id ? { ...section, ...patch } : section,
            ),
          }
        : current,
    )
  }

  async function save(): Promise<boolean> {
    if (!draft) return false
    try {
      setSaveError(null)
      const saved = await update.mutateAsync({
        ...draft,
        sections: draft.sections.map((section) => ({
          ...section,
          evidence: section.evidence.map((item) => item.trim()).filter(Boolean),
        })),
      })
      const latest = queryClient.getQueryData<DraftPlan | null>(draftKeys.plan(draftId))
      if (latest && latest.version > saved.version) {
        // This save landed, but a newer model version already exists. Keep the student's
        // editable text and the comparison instead of rolling the plan back to this response.
        setBaseline(copyPlan(latest))
        setSeenVersion(latest.version)
        setNewerPlan(true)
        toast.info('Your plan was saved. Compare the newer plan before continuing.')
        return false
      }
      const next = copyPlan(saved)
      setDraft(next)
      setBaseline(next)
      setSeenVersion(saved.version)
      setNewerPlan(false)
      toast.success('Plan saved.')
      return true
    } catch {
      setSaveError('Could not save the plan. Your edits are still here; try again.')
      toast.error('Could not save the plan.')
      return false
    }
  }

  return (
    <div className="flex flex-col gap-5">
      {loadError}
      <div className="flex items-center gap-2">
        <div>
          <p className="text-text-primary text-sm font-medium">Plan v{plan.version}</p>
          <p className="text-text-tertiary text-xs" role="status">
            {update.isPending ? 'Saving…' : dirty ? 'Unsaved edits' : 'Saved'}
          </p>
        </div>
        <div className="ml-auto flex gap-1.5">
          <Button
            variant="outline"
            size="sm"
            disabled={update.isPending}
            onClick={() => void save()}
          >
            {update.isPending ? <Spinner /> : <Save className="size-3.5" />}
            Save
          </Button>
          {onRun ? (
            <Button
              size="sm"
              disabled={running || update.isPending}
              onClick={async () => {
                if (await save()) await onRun()
              }}
            >
              <Play className="size-3.5" />
              Continue
            </Button>
          ) : null}
        </div>
      </div>

      {saveError ? (
        <p role="alert" className="text-danger-text text-sm">
          {saveError}
        </p>
      ) : null}
      {newerPlan ? (
        <div className="border-border rounded-md border p-3 text-sm" role="status">
          <p>
            A newer plan is available. Your unsaved edits are preserved. Saving will use your edited
            plan.
          </p>
          <details className="mt-2">
            <summary className="cursor-pointer">Compare with the saved plan</summary>
            <div className="mt-2 space-y-2">
              <p>
                <strong>Brief:</strong> {plan.brief_analysis}
              </p>
              <p>
                <strong>Thesis:</strong> {plan.thesis}
              </p>
              {plan.sections.map((section) => (
                <div key={section.id} className="whitespace-pre-wrap">
                  <p className="font-medium">{section.title}</p>
                  <p>{section.job}</p>
                  <p>{section.claim}</p>
                  <p>{section.evidence.join('\n')}</p>
                  <p>{section.research_notes}</p>
                </div>
              ))}
            </div>
          </details>
        </div>
      ) : null}
      <fieldset disabled={update.isPending} className="flex min-w-0 flex-col gap-5">
        <div className="grid gap-2">
          <Label htmlFor="plan-brief-analysis">What the brief asks</Label>
          <Textarea
            id="plan-brief-analysis"
            value={draft.brief_analysis}
            className="min-h-24"
            onChange={(event) => setDraft({ ...draft, brief_analysis: event.target.value })}
          />
        </div>

        <div className="grid gap-2">
          <Label htmlFor="plan-thesis">Thesis</Label>
          <Textarea
            id="plan-thesis"
            value={draft.thesis}
            className="min-h-24"
            onChange={(event) => setDraft({ ...draft, thesis: event.target.value })}
          />
        </div>

        <section aria-labelledby="plan-argument-heading">
          <h3 id="plan-argument-heading" className="text-text-primary mb-2 text-sm font-medium">
            Argument map
          </h3>
          {draft.argument_map.length > 0 ? (
            <ol className="border-border/70 flex flex-col gap-2 border-l pl-3">
              {draft.argument_map.map((entry, index) => (
                <li key={index} className="text-text-secondary text-sm leading-5">
                  <span className="text-text-tertiary mr-1.5 tabular-nums">{index + 1}.</span>
                  {argumentSummary(entry)}
                </li>
              ))}
            </ol>
          ) : (
            <p className="text-text-tertiary text-sm">No argument map has been saved yet.</p>
          )}
        </section>

        <section aria-labelledby="plan-sections-heading">
          <h3 id="plan-sections-heading" className="text-text-primary mb-2 text-sm font-medium">
            Section jobs
          </h3>
          <ol className="flex flex-col gap-3">
            {draft.sections
              .slice()
              .sort((a, b) => a.ordinal - b.ordinal)
              .map((section) => (
                <li key={section.id} className="border-border rounded-md border p-3">
                  <div className="mb-3 flex items-baseline gap-2">
                    <span className="text-text-tertiary text-xs tabular-nums">
                      §{section.ordinal}
                    </span>
                    <p className="text-text-primary text-sm font-medium">{section.title}</p>
                    {section.word_budget !== null ? (
                      <span className="text-text-tertiary ml-auto text-xs">
                        {section.word_budget} words
                      </span>
                    ) : null}
                  </div>
                  <div className="grid gap-3">
                    <div className="grid gap-1.5">
                      <Label htmlFor={`plan-job-${section.id}`}>Job</Label>
                      <Textarea
                        id={`plan-job-${section.id}`}
                        className="min-h-20"
                        value={section.job}
                        onChange={(event) => changeSection(section.id, { job: event.target.value })}
                      />
                    </div>
                    <div className="grid gap-1.5">
                      <Label htmlFor={`plan-claim-${section.id}`}>Claim</Label>
                      <Textarea
                        id={`plan-claim-${section.id}`}
                        className="min-h-20"
                        value={section.claim}
                        onChange={(event) =>
                          changeSection(section.id, { claim: event.target.value })
                        }
                      />
                    </div>
                    <div className="grid gap-1.5">
                      <Label htmlFor={`plan-evidence-${section.id}`}>
                        Evidence, one item per line
                      </Label>
                      <Textarea
                        id={`plan-evidence-${section.id}`}
                        className="min-h-20"
                        value={section.evidence.join('\n')}
                        onChange={(event) =>
                          changeSection(section.id, {
                            evidence: event.target.value.split('\n'),
                          })
                        }
                      />
                    </div>
                    <div className="grid gap-1.5">
                      <Label htmlFor={`plan-notes-${section.id}`}>Research notes</Label>
                      <Textarea
                        id={`plan-notes-${section.id}`}
                        className="min-h-24"
                        value={section.research_notes}
                        onChange={(event) =>
                          changeSection(section.id, { research_notes: event.target.value })
                        }
                      />
                    </div>
                    <div className="grid gap-1.5">
                      <Label htmlFor={`plan-budget-${section.id}`}>Word budget</Label>
                      <Input
                        id={`plan-budget-${section.id}`}
                        type="number"
                        min={0}
                        value={section.word_budget ?? ''}
                        onChange={(event) => {
                          const value = event.target.value
                          changeSection(section.id, {
                            word_budget: value === '' ? null : Number(value),
                          })
                        }}
                      />
                    </div>
                  </div>
                </li>
              ))}
          </ol>
        </section>
      </fieldset>
    </div>
  )
}

function argumentSummary(entry: DraftPlan['argument_map'][number]): string {
  const claim = typeof entry.claim === 'string' ? entry.claim : null
  const relation = typeof entry.relation === 'string' ? entry.relation : null
  if (claim && relation) return `${claim} · ${relation}`
  if (claim) return claim
  const values = Object.values(entry).filter((value): value is string => typeof value === 'string')
  return values.join(' · ') || 'Untitled claim'
}
