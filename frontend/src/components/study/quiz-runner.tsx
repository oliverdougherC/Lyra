'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ListChecks, MessageSquare } from 'lucide-react'
import Link from 'next/link'
import { toast } from 'sonner'

import { MathText } from '@/components/solutions/math-text'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { Input } from '@/components/ui/input'
import { Progress } from '@/components/ui/progress'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '@/components/ui/spinner'
import { ApiError } from '@/lib/api'
import { chatHandoffUrl, quizMissQuestion, weakTopicQuestion } from '@/lib/handoff'
import { useFinishAttempt, useQuiz, useStartAttempt, useSubmitAnswer } from '@/lib/hooks/use-study'
import { cn } from '@/lib/utils'
import type {
  AnswerRead,
  AttemptRead,
  AttemptResult,
  QuizDifficulty,
  QuizQuestionRead,
} from '@/types'

const DIFFICULTY_LABELS: Record<QuizDifficulty, string> = {
  basic: 'Basic',
  intermediate: 'Intermediate',
  exam: 'Exam',
}

/**
 * One run through a quiz.
 *
 * The attempt is started the moment the questions are known, every answer is graded as it
 * is given, and the summary is whatever `finish` answers - the interface never adds up a
 * score of its own. Grading stays server-side even though the payload carries the answer:
 * what the server records is what the per-topic breakdown is built from, and a client
 * that kept its own tally could disagree with it.
 */
export function QuizRunner({ classId, quizId }: { classId: number; quizId: number }) {
  const quiz = useQuiz(quizId)
  const { mutate: start, isPending: starting } = useStartAttempt(quizId)
  const [attempt, setAttempt] = useState<AttemptRead | null>(null)
  const [startError, setStartError] = useState<string | null>(null)
  const startedRef = useRef(false)

  const { mutateAsync: submitAnswer, isPending: submitting } = useSubmitAnswer(
    attempt?.attempt_id ?? Number.NaN,
  )
  const { mutateAsync: finishAttempt, isPending: finishing } = useFinishAttempt(
    attempt?.attempt_id ?? Number.NaN,
  )

  const [index, setIndex] = useState(0)
  /** The grading of the current question, once given; its presence is the reveal. */
  const [answer, setAnswer] = useState<AnswerRead | null>(null)
  /** The chosen option index, or 0 / -1 for a fill_blank match / miss. */
  const [selected, setSelected] = useState<number | null>(null)
  const [fillText, setFillText] = useState('')
  const [result, setResult] = useState<AttemptResult | null>(null)

  // The attempt fixes the question order; the quiz payload carries what they ask.
  const questions = useMemo(() => {
    if (!quiz.data || !attempt) return []
    const byPart = new Map(quiz.data.questions.map((question) => [question.part_id, question]))
    return attempt.question_part_ids
      .map((partId) => byPart.get(partId))
      .filter((question): question is QuizQuestionRead => question !== undefined)
  }, [quiz.data, attempt])

  const begin = useCallback(
    (restart = false) => {
      startedRef.current = true
      setAttempt(null)
      setStartError(null)
      setIndex(0)
      setAnswer(null)
      setSelected(null)
      setFillText('')
      setResult(null)
      start(restart, {
        onSuccess: (started) => {
          setAttempt(started)
          // Resume where the student left off: the first question with no recorded answer,
          // or the last question when every one has already been answered (PLA-277).
          const answered = new Set(started.answers.map((entry) => entry.part_id))
          const firstUnanswered = started.question_part_ids.findIndex((id) => !answered.has(id))
          setIndex(
            firstUnanswered === -1
              ? Math.max(0, started.question_part_ids.length - 1)
              : firstUnanswered,
          )
        },
        onError: (error) =>
          setStartError(error instanceof ApiError ? error.message : 'Could not start this quiz.'),
      })
    },
    [start],
  )

  // The attempt starts (or resumes) on entry rather than on the first answer, so a quiz
  // abandoned halfway still records how far it got and reopening it continues that attempt.
  useEffect(() => {
    if (quiz.data && !startedRef.current) begin()
  }, [quiz.data, begin])

  if (quiz.isPending) {
    return (
      <div className="flex flex-col gap-4" aria-busy="true" aria-label="Loading quiz">
        <Skeleton className="h-4 w-24" />
        <Skeleton className="h-8 w-3/4" />
        {[0, 1, 2, 3].map((option) => (
          <Skeleton key={option} className="h-12 w-full rounded-md" />
        ))}
      </div>
    )
  }

  if (quiz.isError) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load this quiz</AlertTitle>
        <AlertDescription>
          <p>{quiz.error instanceof ApiError ? quiz.error.message : 'Something went wrong.'}</p>
          <Button variant="outline" size="sm" className="mt-3" onClick={() => void quiz.refetch()}>
            Retry
          </Button>
        </AlertDescription>
      </Alert>
    )
  }

  if (quiz.data.questions.length === 0) {
    return (
      <Empty className="py-12">
        <EmptyHeader>
          <EmptyMedia variant="icon">
            <ListChecks className="text-text-tertiary size-8" />
          </EmptyMedia>
          <EmptyTitle>Nothing to answer</EmptyTitle>
          <EmptyDescription>This quiz has no questions yet.</EmptyDescription>
        </EmptyHeader>
      </Empty>
    )
  }

  if (startError !== null) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not start this quiz</AlertTitle>
        <AlertDescription>
          <p>{startError}</p>
          <Button variant="outline" size="sm" className="mt-3" onClick={() => begin()}>
            Retry
          </Button>
        </AlertDescription>
      </Alert>
    )
  }

  if (result !== null) {
    return <QuizResult classId={classId} result={result} onTryAgain={() => begin(true)} />
  }

  const current = questions[index]
  if (!current) {
    return (
      <div className="flex flex-col items-center gap-3 py-16 text-center" aria-busy="true">
        <Spinner className="text-accent-primary size-6" />
        <p className="text-text-secondary text-sm">
          {starting ? 'Starting your quiz' : 'Preparing the next question'}
        </p>
      </div>
    )
  }

  const payload = current.question
  const revealed = answer !== null
  const isLast = index === questions.length - 1

  async function choose(selectedIndex: number) {
    if (revealed || submitting) return
    setSelected(selectedIndex)
    try {
      setAnswer(await submitAnswer({ part_id: current.part_id, selected_index: selectedIndex }))
    } catch (caught) {
      setSelected(null)
      toast.error(caught instanceof ApiError ? caught.message : 'Could not record that answer.')
    }
  }

  async function checkFillBlank() {
    if (revealed || submitting) return
    // The runner grades the text itself: case-insensitive and whitespace-trimmed against
    // the one stored option, then reported as 0 on a match and -1 on a miss, which is
    // the contract the answers endpoint documents.
    const expected = (payload.options[0] ?? '').trim().toLowerCase()
    const matched = fillText.trim().toLowerCase() === expected
    await choose(matched ? 0 : -1)
  }

  function advance() {
    setIndex((current) => current + 1)
    setAnswer(null)
    setSelected(null)
    setFillText('')
  }

  async function finish() {
    try {
      setResult(await finishAttempt())
    } catch (caught) {
      toast.error(caught instanceof ApiError ? caught.message : 'Could not score this quiz.')
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-3">
        <p className="text-text-tertiary text-sm tabular-nums" aria-live="polite">
          Question {index + 1} of {questions.length}
        </p>
        <div className="flex items-center gap-3">
          <p className="text-text-tertiary text-sm">
            {payload.topic} · {DIFFICULTY_LABELS[payload.difficulty]}
          </p>
          {/* An explicit start-over: the current attempt is retained (abandoned) and a
              fresh one opened, so progress is never discarded implicitly (PLA-277). */}
          <Button
            variant="ghost"
            size="sm"
            className="text-text-tertiary"
            onClick={() => begin(true)}
            disabled={starting}
          >
            Start over
          </Button>
        </div>
      </div>

      <MathText className="text-text-primary text-lg">{payload.question}</MathText>

      {payload.type === 'fill_blank' ? (
        <form
          onSubmit={(event) => {
            event.preventDefault()
            void checkFillBlank()
          }}
          className="flex gap-2"
        >
          <Input
            value={fillText}
            aria-label="Your answer"
            autoComplete="off"
            placeholder="Type the answer"
            disabled={revealed || submitting}
            onChange={(event) => setFillText(event.target.value)}
          />
          {!revealed ? (
            <Button type="submit" disabled={!fillText.trim() || submitting}>
              Check
            </Button>
          ) : null}
        </form>
      ) : (
        <ul className="flex flex-col gap-2">
          {payload.options.map((option, optionIndex) => {
            const isCorrectOption = optionIndex === payload.correct_index
            const isChosen = selected === optionIndex
            return (
              <li key={optionIndex}>
                <button
                  type="button"
                  disabled={revealed || submitting}
                  onClick={() => void choose(optionIndex)}
                  className={cn(
                    'border-border bg-card w-full rounded-md border px-4 py-3 text-left transition-colors',
                    'hover:border-border-strong focus-visible:ring-ring focus-visible:ring-2 focus-visible:outline-none',
                    revealed && isCorrectOption && 'border-success-text bg-success-fill',
                    revealed && isChosen && !isCorrectOption && 'border-danger-text bg-danger-fill',
                    revealed && !isCorrectOption && !isChosen && 'opacity-60',
                  )}
                >
                  <MathText inline>{option}</MathText>
                </button>
              </li>
            )
          })}
        </ul>
      )}

      {answer ? (
        <div
          className={cn(
            'flex flex-col gap-2 rounded-md border p-4',
            answer.correct
              ? 'border-success-text/50 bg-success-fill/40'
              : 'border-danger-text/50 bg-danger-fill/40',
          )}
        >
          <p
            className={cn(
              'text-sm font-medium',
              answer.correct ? 'text-success-text' : 'text-danger-text',
            )}
          >
            {answer.correct ? 'Correct.' : 'Not quite.'}
          </p>
          {!answer.correct ? (
            <div className="text-text-secondary flex items-baseline gap-1.5 text-sm">
              <span>The answer:</span>
              <MathText inline>{payload.options[answer.correct_index]}</MathText>
            </div>
          ) : null}
          <MathText className="text-text-secondary text-sm">{answer.explanation}</MathText>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <Button onClick={() => void (isLast ? finish() : advance())} disabled={finishing}>
              {finishing ? <Spinner /> : null}
              {isLast ? 'See results' : 'Next'}
            </Button>
            {/* A miss is the moment the tutor is worth a click: the question, the wrong
                answer, and the right one travel along, so the conversation opens already
                knowing what went wrong. The explanation above stays; this is for when it
                was not enough.

                Opens in a new tab. The attempt is now durable and resumable (PLA-277), so
                leaving and returning would continue it - but the recorded answers resume,
                not this in-progress reveal, so a new tab still keeps the student's exact
                place. The quiz stays where it is; the label says so. */}
            {!answer.correct ? (
              <Button variant="ghost" asChild>
                <a
                  href={chatHandoffUrl(classId, {
                    ask: quizMissQuestion({
                      topic: payload.topic,
                      question: payload.question,
                      chosen:
                        payload.type === 'fill_blank'
                          ? fillText
                          : selected !== null && selected >= 0
                            ? (payload.options[selected] ?? null)
                            : null,
                      correct: payload.options[answer.correct_index] ?? '',
                    }),
                  })}
                  target="_blank"
                  rel="noopener"
                  aria-label="Go over this with Lyra in a new tab. Your quiz stays open here."
                  title="Opens in a new tab; your quiz stays open here"
                >
                  <MessageSquare aria-hidden className="size-4" />
                  Go over this with Lyra
                  <span aria-hidden className="text-text-tertiary text-xs">
                    new tab
                  </span>
                </a>
              </Button>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  )
}

/** The score and the per-topic breakdown, rendered from the finish response alone. */
function QuizResult({
  classId,
  result,
  onTryAgain,
}: {
  classId: number
  result: AttemptResult
  onTryAgain: () => void
}) {
  return (
    <section aria-label="Quiz results" className="flex flex-col gap-6 py-4">
      <div className="flex flex-col items-center gap-1 text-center">
        <h2 className="font-heading text-text-primary text-2xl tracking-tight">
          You scored {result.score} out of {result.total}
        </h2>
        <p className="text-text-secondary text-sm">Anything under 60% wants another look.</p>
      </div>

      <ul className="flex flex-col gap-3">
        {result.by_topic.map((entry) => {
          const ratio = entry.total > 0 ? entry.correct / entry.total : 0
          const weak = ratio < 0.6
          return (
            <li key={entry.topic} className="flex flex-col gap-1">
              <div className="flex items-baseline justify-between gap-3">
                <span className={cn('text-sm font-medium', weak ? 'text-danger-text' : undefined)}>
                  {entry.topic}
                </span>
                <span
                  className={cn(
                    'text-xs tabular-nums',
                    weak ? 'text-danger-text' : 'text-text-tertiary',
                  )}
                >
                  {entry.correct} of {entry.total}
                </span>
              </div>
              <Progress
                value={ratio * 100}
                aria-label={`${entry.topic}: ${entry.correct} of ${entry.total} correct`}
                className={weak ? '[&_[data-slot=progress-indicator]]:bg-danger-text' : undefined}
              />
              {/* A weak topic is a question waiting to be asked. The words travel to the
                  tutor's composer, where the student can still change them before asking. */}
              {weak ? (
                <Link
                  href={chatHandoffUrl(classId, { ask: weakTopicQuestion(entry.topic) })}
                  className="text-text-secondary hover:text-accent-primary focus-visible:ring-ring inline-flex items-center gap-1.5 self-start rounded-sm text-xs transition-colors focus-visible:ring-2 focus-visible:outline-none"
                >
                  <MessageSquare aria-hidden className="size-3" />
                  Go over this with Lyra
                </Link>
              ) : null}
            </li>
          )
        })}
      </ul>

      <div className="flex justify-center">
        <Button variant="outline" onClick={onTryAgain}>
          Try again
        </Button>
      </div>
    </section>
  )
}
