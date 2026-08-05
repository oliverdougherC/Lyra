'use client'

import { CheckCircle2, HelpCircle } from 'lucide-react'

import { FactRow } from '@/components/profile/fact-row'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty'
import { Skeleton } from '@/components/ui/skeleton'
import { useClassProfile, useCorrectFact, useResolveFact } from '@/lib/hooks/use-profile'
import type { FactKind } from '@/types'

const SECTIONS: { kind: FactKind; title: string }[] = [
  { kind: 'deadline', title: 'Deadlines' },
  { kind: 'topic', title: 'Topics' },
  { kind: 'grading', title: 'Grading' },
  { kind: 'professor', title: 'Professor' },
  { kind: 'prerequisite', title: 'Prerequisites' },
  // "Other details" was a heading for a bucket that had become one, and naming it that way
  // invited everything unclassifiable into it. A `note` is now a convention the course
  // holds, which is the one thing in here a tutor would otherwise get wrong.
  { kind: 'note', title: 'Course conventions' },
]

const SKIPPED_COPY = {
  extraction_disabled: 'Automatic profile extraction is turned off in Settings.',
  no_endpoint: 'Add a tutor endpoint in Settings before Lyra can analyze your syllabus.',
  remote_unacknowledged:
    'Profile extraction is paused because this endpoint is remote. Acknowledge sending document text in Settings to continue.',
  unparseable_response: 'Lyra could not read the analysis response for the most recent upload.',
} as const

/**
 * What Lyra has worked out about a class, in sections, every value editable.
 *
 * Written as a body rather than as a panel so the same list serves both surfaces it is
 * wanted on: the header's sheet, which is how a student checks a fact without leaving the
 * conversation, and the class hub's own tab, which is where they go to correct one. Two
 * implementations of an editable fact list would drift, and the one in the sheet is the
 * one nobody would remember to update.
 */
export function ProfileFacts({ classId, enabled = true }: { classId: number; enabled?: boolean }) {
  const { data: profile, isPending, isError, error, refetch } = useClassProfile(classId, enabled)
  const correctFact = useCorrectFact(classId)
  const resolveFact = useResolveFact(classId)

  if (isPending) {
    return (
      <div className="space-y-3" aria-busy="true" aria-label="Loading class profile">
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-20 w-full" />
      </div>
    )
  }

  if (isError) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load the class profile</AlertTitle>
        <AlertDescription>
          <p>{error instanceof Error ? error.message : 'Try loading the profile again.'}</p>
          <Button variant="outline" size="sm" className="mt-3" onClick={() => void refetch()}>
            Retry
          </Button>
        </AlertDescription>
      </Alert>
    )
  }

  if (profile?.extraction_skipped_reason) {
    return (
      <Alert className="border-info-text bg-info-fill">
        <HelpCircle aria-hidden className="text-info-text" />
        <AlertTitle className="text-info-text">Profile extraction is paused</AlertTitle>
        <AlertDescription className="text-info-text">
          {SKIPPED_COPY[profile.extraction_skipped_reason]}
        </AlertDescription>
      </Alert>
    )
  }

  if ((profile?.facts.length ?? 0) === 0) {
    return (
      <Empty className="py-12 text-left">
        <EmptyHeader>
          <EmptyTitle>No profile yet</EmptyTitle>
          <EmptyDescription>
            Upload a syllabus and Lyra will pull out dates, topics, and grading.
          </EmptyDescription>
        </EmptyHeader>
      </Empty>
    )
  }

  const busy = correctFact.isPending || resolveFact.isPending

  return (
    <div className="space-y-8">
      {SECTIONS.map(({ kind, title }) => {
        const facts = profile?.facts.filter((fact) => fact.kind === kind) ?? []
        if (facts.length === 0) return null
        return (
          <section key={kind} aria-labelledby={`profile-${kind}`}>
            <h3
              id={`profile-${kind}`}
              className="mb-3 border-b pb-2 text-xs font-medium tracking-[0.14em] uppercase"
            >
              {title}
            </h3>
            <ul className="overflow-hidden rounded-md border bg-card">
              {facts.map((fact) => (
                <FactRow
                  key={fact.id}
                  fact={fact}
                  busy={busy}
                  onCorrect={(value) => correctFact.mutate({ factId: fact.id, value })}
                  onResolve={(action) => resolveFact.mutate({ factId: fact.id, action })}
                />
              ))}
            </ul>
          </section>
        )
      })}
      <p className="text-text-tertiary flex items-start gap-2 text-xs">
        <CheckCircle2 aria-hidden className="mt-px size-4 shrink-0" />
        Lyra found these in your documents and uses them in answers. Click any value to correct it.
      </p>
    </div>
  )
}
