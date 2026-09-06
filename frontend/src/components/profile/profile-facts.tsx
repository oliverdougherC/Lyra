'use client'

import { HelpCircle } from 'lucide-react'
import Link from '@/router/link'

import { FactRow } from '@/components/profile/fact-row'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty'
import { Skeleton } from '@/components/ui/skeleton'
import { useClassProfile, useCorrectFact, useResolveFact } from '@/lib/hooks/use-profile'
import type { ExtractionSkipReason, FactKind, FactRead } from '@/types'

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
    href: '/settings#extraction-enabled',
  },
  no_endpoint: {
    title: 'Profile extraction is paused',
    body: 'Add a tutor endpoint in Settings before Lyra can analyze your syllabus.',
    settings: 'Open settings',
    href: '/settings#endpoint-url',
  },
  remote_unacknowledged: {
    title: 'Profile extraction is paused',
    body: 'Profile extraction is paused because this endpoint is remote. Acknowledge sending document text in Settings to continue.',
    settings: 'Open settings',
    href: '/settings#remote-ack',
  },
  unparseable_response: {
    title: 'Profile extraction did not finish',
    body: 'Lyra could not read the analysis response for the most recent upload.',
    settings: null,
    href: null,
  },
  endpoint_failed: {
    title: 'The tutor endpoint could not analyze this upload',
    body: 'The endpoint answered with an error. This usually means the server has a different model loaded than the one Lyra is set to use, or one whose context window is smaller than the setting says.',
    settings: 'Check endpoint settings',
    href: '/settings#endpoint-url',
  },
  extraction_failed: {
    title: 'Profile extraction did not finish',
    body: 'Analysis of the most recent upload did not complete. The document itself is uploaded and searchable.',
    settings: null,
    href: null,
  },
} as const satisfies Record<
  ExtractionSkipReason,
  { title: string; body: string; settings: string | null; href: string | null }
>

// The endpoint may be newer than this UI, or return an incomplete cached payload.
// Only rows with usable values and status metadata may be offered for editing.
function isUsableFact(value: unknown): value is FactRead {
  if (!value || typeof value !== 'object') return false
  const fact = value as Partial<FactRead>
  return (
    typeof fact.id === 'number' &&
    Number.isSafeInteger(fact.id) &&
    SECTIONS.some(({ kind }) => kind === fact.kind) &&
    typeof fact.value === 'string' &&
    fact.value.trim().length > 0 &&
    typeof fact.label === 'string' &&
    typeof fact.confirmed === 'boolean' &&
    typeof fact.rejected === 'boolean' &&
    ['low', 'high'].includes(fact.confidence ?? '') &&
    Array.isArray(fact.sources) &&
    fact.sources.every((source) => typeof source === 'string') &&
    (fact.source_url == null || typeof fact.source_url === 'string')
  )
}

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

  const rawFacts: unknown = profile?.facts
  const facts = Array.isArray(rawFacts) ? rawFacts.filter(isUsableFact) : []
  const malformed =
    (!isPending && !isError && profile == null) ||
    (profile != null && (!Array.isArray(rawFacts) || rawFacts.length !== facts.length))
  const reason: unknown = profile?.extraction_skipped_reason
  const skipped =
    reason == null || reason === ''
      ? null
      : typeof reason === 'string' && Object.hasOwn(SKIPPED, reason)
        ? SKIPPED[reason as ExtractionSkipReason]
        : {
            title: 'Profile extraction did not finish',
            body: 'Lyra could not recognize the latest extraction status. Try uploading again to update the profile.',
            settings: null,
            href: null,
          }
  const busy = correctFact.isPending || resolveFact.isPending
  const editError = correctFact.isError || resolveFact.isError

  if (isPending && facts.length === 0) {
    return (
      <div className="space-y-3" aria-busy="true" aria-label="Loading class profile">
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-20 w-full" />
      </div>
    )
  }

  return (
    <div className="space-y-8">
      {isError ? (
        <Alert variant="destructive">
          <AlertTitle>
            {facts.length
              ? 'Could not refresh the class profile'
              : 'Could not load the class profile'}
          </AlertTitle>
          <AlertDescription>
            <p>
              {facts.length
                ? 'Showing saved details. Try again to check for updates.'
                : error instanceof Error
                  ? error.message
                  : 'Try loading the profile again.'}
            </p>
            <Button variant="outline" size="sm" className="mt-3" onClick={() => void refetch()}>
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}
      {malformed ? (
        <Alert variant="destructive">
          <AlertTitle>Some profile details could not be read</AlertTitle>
          <AlertDescription>
            <p>Try loading the profile again to recover missing details.</p>
            <Button variant="outline" size="sm" className="mt-3" onClick={() => void refetch()}>
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}
      {skipped ? (
        <Alert className="border-info-text bg-info-fill">
          <HelpCircle aria-hidden className="text-info-text" />
          <AlertTitle className="text-info-text">{skipped.title}</AlertTitle>
          <AlertDescription className="text-info-text">
            <p>{skipped.body}</p>
            {facts.length ? <p>Your saved profile details are still available below.</p> : null}
            {skipped.settings && skipped.href ? (
              <p className="mt-2">
                <Link href={skipped.href} className="underline underline-offset-2">
                  {skipped.settings}
                </Link>
              </p>
            ) : null}
          </AlertDescription>
        </Alert>
      ) : null}
      {editError ? (
        <Alert variant="destructive">
          <AlertTitle>Could not save the profile change</AlertTitle>
          <AlertDescription>
            Your saved details are unchanged. Try editing or confirming the fact again.
          </AlertDescription>
        </Alert>
      ) : null}
      {facts.length === 0 && !isError && !malformed && !skipped ? (
        <Empty className="py-12">
          <EmptyHeader>
            <EmptyTitle>No profile yet</EmptyTitle>
            <EmptyDescription>
              Upload a syllabus and Lyra will pull out dates, topics, and grading.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : null}
      {SECTIONS.map(({ kind, title }) => {
        const sectionFacts = facts.filter((fact) => fact.kind === kind)
        if (sectionFacts.length === 0) return null
        return (
          <section key={kind} aria-labelledby={`profile-${kind}`}>
            <h3
              id={`profile-${kind}`}
              className="mb-3 border-b pb-2 text-xs font-medium tracking-[0.14em] uppercase"
            >
              {title}
            </h3>
            <ul className="overflow-hidden rounded-md border bg-card">
              {sectionFacts.map((fact) => (
                <FactRow
                  key={fact.id}
                  fact={fact}
                  busy={busy}
                  onCorrect={(value) => {
                    resolveFact.reset()
                    correctFact.mutate({ factId: fact.id, value })
                  }}
                  onResolve={(action) => {
                    correctFact.reset()
                    resolveFact.mutate({ factId: fact.id, action })
                  }}
                />
              ))}
            </ul>
          </section>
        )
      })}
      {facts.length > 0 ? (
        <p className="text-text-tertiary flex items-start gap-2 text-xs">
          Review details found in your documents. Click any value to correct it.
        </p>
      ) : null}
    </div>
  )
}
