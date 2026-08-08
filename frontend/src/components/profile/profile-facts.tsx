'use client'

import { CheckCircle2, HelpCircle } from 'lucide-react'
import Link from 'next/link'

import { FactRow } from '@/components/profile/fact-row'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty'
import { Skeleton } from '@/components/ui/skeleton'
import { useClassProfile, useCorrectFact, useResolveFact } from '@/lib/hooks/use-profile'
import type { ExtractionSkipReason, FactKind } from '@/types'

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

/**
 * What each reason the `extracting` stage did not run reads as.
 *
 * `settings` marks the ones the student fixes somewhere rather than by uploading anything
 * else, and is a link rather than a sentence because a reason with an address should take
 * you there. `endpoint_failed` is the only one whose cause Lyra cannot name outright: a
 * local runtime that refuses the largest prompt Lyra builds is usually holding a different
 * model than the settings describe, so the copy says that and sends them to the two fields
 * that decide it. The server's own words for it are in the log, not here.
 */
const SKIPPED = {
  extraction_disabled: {
    title: 'Profile extraction is paused',
    body: 'Automatic profile extraction is turned off in Settings.',
    settings: 'Open settings',
  },
  no_endpoint: {
    title: 'Profile extraction is paused',
    body: 'Add a tutor endpoint in Settings before Lyra can analyze your syllabus.',
    settings: 'Open settings',
  },
  remote_unacknowledged: {
    title: 'Profile extraction is paused',
    body: 'Profile extraction is paused because this endpoint is remote. Acknowledge sending document text in Settings to continue.',
    settings: 'Open settings',
  },
  unparseable_response: {
    title: 'Profile extraction did not finish',
    body: 'Lyra could not read the analysis response for the most recent upload.',
    settings: null,
  },
  endpoint_failed: {
    title: 'The tutor endpoint could not analyze this upload',
    body: 'The endpoint answered with an error. This usually means the server has a different model loaded than the one Lyra is set to use, or one whose context window is smaller than the setting says.',
    settings: 'Check the model and context window',
  },
  extraction_failed: {
    title: 'Profile extraction did not finish',
    body: 'Analysis of the most recent upload did not complete. The document itself is uploaded and searchable.',
    settings: null,
  },
} as const satisfies Record<
  ExtractionSkipReason,
  { title: string; body: string; settings: string | null }
>

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
    const skipped = SKIPPED[profile.extraction_skipped_reason]
    return (
      <Alert className="border-info-text bg-info-fill">
        <HelpCircle aria-hidden className="text-info-text" />
        <AlertTitle className="text-info-text">{skipped.title}</AlertTitle>
        <AlertDescription className="text-info-text">
          <p>{skipped.body}</p>
          {skipped.settings ? (
            <p className="mt-2">
              <Link href="/settings#model" className="underline underline-offset-2">
                {skipped.settings}
              </Link>
            </p>
          ) : null}
        </AlertDescription>
      </Alert>
    )
  }

  if ((profile?.facts.length ?? 0) === 0) {
    return (
      <Empty className="py-12">
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
