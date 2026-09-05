'use client'

import { useQueryClient } from '@tanstack/react-query'
import { ArrowLeft } from 'lucide-react'
import Link from '@/router/link'
import { useParams } from '@/router/hooks'
import { useEffect, useMemo } from 'react'

import { LyraMark } from '@/components/chat/lyra-mark'
import { DeckSession } from '@/components/study/deck-session'
import { QuizRunner } from '@/components/study/quiz-runner'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import { Skeleton } from '@/components/ui/skeleton'
import { ApiError } from '@/lib/api'
import {
  isGenerating,
  studyKeys,
  useDeckStatus,
  useQuizStatus,
  useStudyList,
} from '@/lib/hooks/use-study'

function readId(value: string | string[] | undefined): number | null {
  const raw = Array.isArray(value) ? value[0] : value
  const parsed = Number(raw)
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null
}

export default function StudySessionPage() {
  const params = useParams<{ id: string; artifactId: string }>()
  const classId = readId(params.id)
  const artifactId = readId(params.artifactId)
  const queryClient = useQueryClient()

  // The study list answers which kind this id is, so the detail endpoints keep their
  // kind guards instead of the interface guessing one and catching the other's 404.
  const studyList = useStudyList(classId ?? Number.NaN, classId !== null)
  const entry = useMemo(() => {
    if (!studyList.data || artifactId === null) return undefined
    return (
      studyList.data.decks.find((deck) => deck.id === artifactId) ??
      studyList.data.quizzes.find((quiz) => quiz.id === artifactId)
    )
  }, [studyList.data, artifactId])
  const kind = entry?.kind ?? null

  const deckStatus = useDeckStatus(
    kind === 'flashcard_deck' ? (artifactId ?? Number.NaN) : Number.NaN,
  )
  const quizStatus = useQuizStatus(kind === 'quiz' ? (artifactId ?? Number.NaN) : Number.NaN)
  const status = kind === 'quiz' ? quizStatus : deckStatus

  // The poll is the live source of truth for state; when it moves, the detail and the
  // list are stale, exactly as the solutions page treats its own poll.
  const polledState = status.data?.state
  useEffect(() => {
    if (!polledState || classId === null || artifactId === null) return
    if (kind === 'flashcard_deck') {
      queryClient.invalidateQueries({ queryKey: studyKeys.deck(artifactId) })
    }
    if (kind === 'quiz') {
      queryClient.invalidateQueries({ queryKey: studyKeys.quiz(artifactId) })
    }
    queryClient.invalidateQueries({ queryKey: studyKeys.list(classId) })
  }, [polledState, kind, classId, artifactId, queryClient])

  if (classId === null || artifactId === null) {
    return (
      <Alert variant="destructive">
        <AlertTitle>That link is not valid</AlertTitle>
        <AlertDescription>Open a deck or quiz from your class workspace.</AlertDescription>
      </Alert>
    )
  }

  if (studyList.isPending) {
    return (
      <div className="mx-auto flex w-full max-w-[720px] flex-col gap-4" aria-busy="true">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-4 w-96" />
        <Skeleton className="h-64 w-full rounded-md" />
      </div>
    )
  }

  if (studyList.isError) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load this study tool</AlertTitle>
        <AlertDescription>
          <p>
            {studyList.error instanceof ApiError
              ? studyList.error.message
              : 'Something went wrong.'}
          </p>
          <Button
            variant="outline"
            size="sm"
            className="mt-3"
            onClick={() => void studyList.refetch()}
          >
            Retry
          </Button>
        </AlertDescription>
      </Alert>
    )
  }

  if (!entry) {
    return (
      <Alert variant="destructive">
        <AlertTitle>That study tool is not in this class</AlertTitle>
        <AlertDescription>
          <p>It may have been deleted. Open another one from the workspace.</p>
          <Button asChild variant="outline" size="sm" className="mt-3">
            <Link href={`/classes/${classId}?tab=practice`}>Back to practice</Link>
          </Button>
        </AlertDescription>
      </Alert>
    )
  }

  // The poll is fresher than the list row, so state comes from it once it has landed.
  const state = polledState ?? entry.state
  const stageDetail = status.data?.stage_detail ?? entry.stage_detail
  const problemsTotal = status.data?.problems_total ?? entry.problems_total
  const problemsDone = status.data?.problems_done ?? entry.problems_done
  const errorMessage = status.data?.error_message ?? entry.error_message

  return (
    <div className="mx-auto flex w-full max-w-[720px] flex-col gap-6">
      <header className="flex flex-wrap items-center gap-3 pt-2 md:pt-6">
        <Button asChild variant="ghost" size="sm" className="-ml-2">
          <Link href={`/classes/${classId}?tab=practice`}>
            <ArrowLeft className="size-4" />
            Practice
          </Link>
        </Button>
        <h1 className="font-heading text-text-primary min-w-0 truncate text-2xl tracking-tight">
          {entry.title}
        </h1>
      </header>

      {isGenerating(state) ? (
        <div
          className="flex flex-col items-center gap-4 py-16 text-center"
          aria-busy="true"
          aria-label={`Generating ${entry.title}`}
        >
          <span className="text-accent-primary size-8">
            <LyraMark thinking />
          </span>
          <div className="flex flex-col gap-1">
            <p className="text-text-primary text-base font-medium">
              {state === 'pending' ? 'Queued' : 'Writing'}
            </p>
            {stageDetail ? <p className="text-text-secondary text-sm">{stageDetail}</p> : null}
          </div>
          {/* No bar until the count is real: a bar sitting at zero implies a denominator
              nobody has computed yet, the same rule SolveProgress keeps. */}
          {problemsTotal !== null && problemsTotal > 0 ? (
            <Progress
              value={(problemsDone / problemsTotal) * 100}
              className="w-64"
              aria-label={`${problemsDone} of ${problemsTotal} written`}
            />
          ) : null}
        </div>
      ) : null}

      {state === 'failed' ? (
        <Alert variant="destructive">
          <AlertTitle>Lyra could not finish writing this</AlertTitle>
          <AlertDescription>
            <p>{errorMessage ?? 'Something went wrong while working on it.'}</p>
            <Button asChild variant="outline" size="sm" className="mt-3">
              <Link href={`/classes/${classId}?tab=practice`}>Back to practice</Link>
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}

      {state === 'cancelled' ? (
        <Alert>
          <AlertTitle>This was cancelled</AlertTitle>
          <AlertDescription>
            <p>Generation was stopped before it finished.</p>
            <Button asChild variant="outline" size="sm" className="mt-3">
              <Link href={`/classes/${classId}?tab=practice`}>Back to practice</Link>
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}

      {state === 'ready' && kind === 'flashcard_deck' ? (
        <DeckSession key={artifactId} deckId={artifactId} />
      ) : null}
      {state === 'ready' && kind === 'quiz' ? (
        <QuizRunner key={artifactId} classId={classId} quizId={artifactId} />
      ) : null}
    </div>
  )
}
