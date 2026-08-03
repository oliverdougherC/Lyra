'use client'

import { CheckCircle2, HelpCircle } from 'lucide-react'

import { FactRow } from '@/components/profile/fact-row'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { useClassProfile, useCorrectFact, useResolveFact } from '@/lib/hooks/use-profile'
import { cn } from '@/lib/utils'
import type { FactKind } from '@/types'

const SECTIONS: { kind: FactKind; title: string }[] = [
  { kind: 'deadline', title: 'Deadlines' },
  { kind: 'topic', title: 'Topics' },
  { kind: 'grading', title: 'Grading' },
  { kind: 'professor', title: 'Professor' },
  { kind: 'prerequisite', title: 'Prerequisites' },
]

const SKIPPED_COPY = {
  extraction_disabled: 'Automatic profile extraction is turned off in Settings.',
  no_endpoint: 'Add a tutor endpoint in Settings before Lyra can analyze your syllabus.',
  remote_unacknowledged:
    'Profile extraction is paused because this endpoint is remote. Acknowledge sending document text in Settings to continue.',
  unparseable_response: 'Lyra could not read the analysis response for the most recent upload.',
} as const

export function ClassProfileSheet({
  classId,
  open,
  onOpenChange,
}: {
  classId: number
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { data: profile, isPending, isError, error, refetch } = useClassProfile(classId, open)
  const correctFact = useCorrectFact(classId)
  const resolveFact = useResolveFact(classId)

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-full sm:max-w-md">
        <SheetHeader className="border-b px-5 py-4">
          <SheetTitle>Class profile</SheetTitle>
          <SheetDescription>Facts Lyra found in your course materials.</SheetDescription>
        </SheetHeader>
        <ScrollArea className="h-[calc(100vh-9rem)] px-5 py-5">
          {isPending ? (
            <div className="space-y-3" aria-busy="true" aria-label="Loading class profile">
              <Skeleton className="h-20 w-full" />
              <Skeleton className="h-20 w-full" />
              <Skeleton className="h-20 w-full" />
            </div>
          ) : isError ? (
            <Alert variant="destructive">
              <AlertTitle>Could not load the class profile</AlertTitle>
              <AlertDescription>
                <p>{error instanceof Error ? error.message : 'Try loading the profile again.'}</p>
                <Button variant="outline" size="sm" className="mt-3" onClick={() => void refetch()}>
                  Retry
                </Button>
              </AlertDescription>
            </Alert>
          ) : profile?.extraction_skipped_reason ? (
            <Alert className="border-info-text bg-info-fill">
              <HelpCircle aria-hidden className="text-info-text" />
              <AlertTitle className="text-info-text">Profile extraction is paused</AlertTitle>
              <AlertDescription className="text-info-text">
                {SKIPPED_COPY[profile.extraction_skipped_reason]}
              </AlertDescription>
            </Alert>
          ) : (profile?.facts.length ?? 0) === 0 ? (
            <Empty className="py-12 text-left">
              <EmptyHeader>
                <EmptyTitle>No profile yet</EmptyTitle>
                <EmptyDescription>
                  Upload a syllabus and Lyra will pull out dates, topics, and grading.
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          ) : (
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
                      {facts.map((fact) => {
                        const busy = correctFact.isPending || resolveFact.isPending
                        return (
                          <FactRow
                            key={fact.id}
                            fact={fact}
                            busy={busy}
                            onCorrect={(value) => correctFact.mutate({ factId: fact.id, value })}
                            onResolve={(action) => resolveFact.mutate({ factId: fact.id, action })}
                          />
                        )
                      })}
                    </ul>
                  </section>
                )
              })}
              <p className={cn('flex items-center gap-2 text-xs text-text-tertiary')}>
                <CheckCircle2 aria-hidden className="size-4 text-success-text" />
                Confirmed and high-confidence facts guide future answers.
              </p>
            </div>
          )}
        </ScrollArea>
      </SheetContent>
    </Sheet>
  )
}
