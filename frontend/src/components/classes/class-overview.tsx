'use client'

import { useMemo, useState } from 'react'
import { ArrowRight, FileUp, Layers, PenLine, SquareCheckBig } from 'lucide-react'
import Link from '@/router/link'
import { useRouter } from '@/router/hooks'
import { toast } from 'sonner'

import { StatusWord } from '@/components/ex-libris'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Spinner } from '@/components/ui/spinner'
import { ApiError } from '@/lib/api'
import { buildSuggestedPrompts } from '@/components/chat/suggested-prompts'
import { formatRelativeTime, formatSessionFallbackTitle } from '@/lib/format'
import { chatHandoffUrl, quickStudyTitle, untitledDraftTitle } from '@/lib/handoff'
import { useSettings } from '@/lib/hooks/use-settings'
import { useSessions } from '@/lib/hooks/use-chat'
import { isTerminal, needsAttention, useDocuments } from '@/lib/hooks/use-documents'
import { useCreateDraft, useDrafts } from '@/lib/hooks/use-drafts'
import { useClassProfile } from '@/lib/hooks/use-profile'
import { useSolutions } from '@/lib/hooks/use-solutions'
import { isGenerating, useCreateQuiz, useStudyList } from '@/lib/hooks/use-study'
import type { ArtifactState } from '@/types'

/**
 * One thing the student can go back to, shown on the class landing.
 *
 * The rows answer "where was I?" and "what needs me?" rather than restating what the tabs
 * already list. Anything in flight, waiting, or broken outranks anything merely recent,
 * because the recent thing will still be recent after the broken thing is dealt with.
 */
type ContinueItem = {
  key: string
  href: string
  title: string
  /** The status, in words. Null for rows whose being listed is the whole message. */
  word: { tone: 'nominal' | 'active' | 'warn' | 'info'; text: string } | null
  /** A quiet relative timestamp, for resume rows. */
  time: string | null
}

/** How many rows the continue list shows before it defers to the tabs. */
const CONTINUE_ROWS = 6

function isWorking(state: ArtifactState): boolean {
  return (
    state === 'pending' || state === 'generating' || state === 'segmenting' || state === 'solving'
  )
}

/**
 * The class landing: what do you want to work on?
 *
 * The old overview was a table of contents for the other tabs, each section repeating the
 * first rows of a list one click away. This one starts work instead: a question goes to
 * the tutor from here, unfinished work is one click from where it was left, and the
 * common ways to start something are a quiet row of verbs rather than a feature index.
 * Browsing and managing stay in the tabs, which is what tabs are for.
 */
export function ClassOverview({ classId, className }: { classId: number; className?: string }) {
  const router = useRouter()
  const { data: sessions } = useSessions(classId)
  const { data: solutions } = useSolutions(classId)
  const { data: study } = useStudyList(classId)
  const { data: drafts } = useDrafts(classId)
  const settingsQuery = useSettings()
  const tutorReady = Boolean(settingsQuery.data?.endpoint_url)
  const needsTutorSetup = settingsQuery.isSuccess && !tutorReady
  const documentsQuery = useDocuments(classId)
  const documents = documentsQuery.data
  const { data: profile } = useClassProfile(classId)
  const createQuiz = useCreateQuiz(classId)
  const createDraft = useCreateDraft(classId)

  const [question, setQuestion] = useState('')

  // Distinct facts, kept distinct: no documents at all, documents still being read,
  // documents Lyra cannot use, and documents ready to work from can all be true at once,
  // and the copy below must never collapse them into one guess.
  const documentsLoaded = documents !== undefined
  const readyCount = documents?.filter((document) => document.state === 'ready').length ?? 0
  const ingestingCount = documents?.filter((document) => !isTerminal(document.state)).length ?? 0
  const attentionCount = documents?.filter((document) => needsAttention(document.state)).length ?? 0
  const suggestions = useMemo(() => buildSuggestedPrompts(profile?.facts ?? []), [profile?.facts])

  const items = useMemo<ContinueItem[]>(() => {
    const waiting: ContinueItem[] = []
    const broken: ContinueItem[] = []
    const working: ContinueItem[] = []
    const due: ContinueItem[] = []
    const resume: ContinueItem[] = []

    for (const solution of solutions ?? []) {
      const href = `/classes/${classId}/solutions/${solution.id}`
      if (solution.state === 'awaiting_review') {
        waiting.push({
          key: `solution-${solution.id}`,
          href,
          title: solution.title,
          word: { tone: 'info', text: 'Waiting for your check' },
          time: null,
        })
      } else if (solution.state === 'failed') {
        broken.push({
          key: `solution-${solution.id}`,
          href,
          title: solution.title,
          word: { tone: 'warn', text: 'Could not finish' },
          time: null,
        })
      } else if (isWorking(solution.state)) {
        working.push({
          key: `solution-${solution.id}`,
          href,
          title: solution.title,
          word: { tone: 'nominal', text: solution.stage_detail ?? 'Solving' },
          time: null,
        })
      }
    }

    const studyArtifacts = [...(study?.decks ?? []), ...(study?.quizzes ?? [])]
    for (const artifact of studyArtifacts) {
      const href = `/classes/${classId}/study/${artifact.id}`
      if (artifact.state === 'failed') {
        broken.push({
          key: `study-${artifact.id}`,
          href,
          title: artifact.title,
          word: { tone: 'warn', text: 'Could not finish' },
          time: null,
        })
      } else if (isGenerating(artifact.state)) {
        working.push({
          key: `study-${artifact.id}`,
          href,
          title: artifact.title,
          word: { tone: 'nominal', text: artifact.stage_detail ?? 'Writing' },
          time: null,
        })
      }
    }

    for (const draft of drafts ?? []) {
      const href = `/classes/${classId}/drafts/${draft.id}`
      if (draft.state === 'failed') {
        broken.push({
          key: `draft-${draft.id}`,
          href,
          title: draft.title,
          word: { tone: 'warn', text: 'Could not finish' },
          time: null,
        })
      } else if (isWorking(draft.state)) {
        working.push({
          key: `draft-${draft.id}`,
          href,
          title: draft.title,
          word: { tone: 'nominal', text: draft.stage_detail ?? 'Suggesting' },
          time: null,
        })
      }
    }

    // Aggregated rather than one row per file: a folder drop that failed wholesale
    // should be one line saying so, not a screen of red (the Documents tab has the
    // per-file detail and the retry affordances). "Could not be used" covers both ways
    // a document ends up unusable, a failure during ingestion and a format Lyra does
    // not read; the tab tells them apart.
    if (attentionCount > 0) {
      broken.push({
        key: 'documents-attention',
        href: `/classes/${classId}?tab=files`,
        title:
          attentionCount === 1
            ? 'One document could not be used'
            : `${attentionCount} documents could not be used`,
        word: { tone: 'warn', text: 'Needs attention' },
        time: null,
      })
    }

    if (ingestingCount > 0) {
      working.push({
        key: 'documents-ingesting',
        href: `/classes/${classId}?tab=files`,
        title:
          ingestingCount === 1
            ? 'One document being read'
            : `${ingestingCount} documents being read`,
        word: null,
        time: null,
      })
    }

    const dueDecks = (study?.decks ?? []).filter(
      (deck) => deck.state === 'ready' && deck.due_count > 0,
    )
    if (dueDecks.length === 1) {
      due.push({
        key: `due-${dueDecks[0].id}`,
        href: `/classes/${classId}/study/${dueDecks[0].id}`,
        title: dueDecks[0].title,
        word: { tone: 'info', text: `${dueDecks[0].due_count} cards due` },
        time: null,
      })
    } else if (dueDecks.length > 1) {
      const total = dueDecks.reduce((sum, deck) => sum + deck.due_count, 0)
      due.push({
        key: 'due-all',
        href: `/classes/${classId}?tab=practice`,
        title: 'Review what is due',
        word: { tone: 'info', text: `${total} cards across ${dueDecks.length} decks` },
        time: null,
      })
    }

    // Ways back into existing work, ordered by the timestamps the backend actually
    // records (a session's creation, an artifact's last change) and capped so the list
    // stays a glance. Rows already listed above (waiting, broken, working, due) do not
    // repeat.
    const listed = new Set([...waiting, ...broken, ...working, ...due].map((item) => item.key))
    const candidates: { at: string; item: ContinueItem }[] = []
    const newestSession =
      sessions && sessions.length > 0 ? [...sessions].sort((a, b) => b.id - a.id)[0] : null
    if (newestSession) {
      candidates.push({
        at: newestSession.created_at,
        item: {
          key: `session-${newestSession.id}`,
          href: `/classes/${classId}/chat?session=${newestSession.id}`,
          title: newestSession.title || formatSessionFallbackTitle(newestSession.created_at),
          word: null,
          // Sessions carry only a creation time, so that is the only claim made. Saying
          // when a conversation was last *active* would need data the backend does not
          // record.
          time: `Started ${formatRelativeTime(newestSession.created_at)}`,
        },
      })
    }
    for (const solution of (solutions ?? []).filter((item) => item.state === 'ready')) {
      candidates.push({
        at: solution.updated_at,
        item: {
          key: `solution-${solution.id}`,
          href: `/classes/${classId}/solutions/${solution.id}`,
          title: solution.title,
          word: null,
          // `updated_at` moves when the work changed, not when the student last looked;
          // "Updated" is the honest verb for that.
          time: `Updated ${formatRelativeTime(solution.updated_at)}`,
        },
      })
    }
    for (const draft of (drafts ?? []).filter((item) => item.state === 'ready')) {
      candidates.push({
        at: draft.updated_at,
        item: {
          key: `draft-${draft.id}`,
          href: `/classes/${classId}/drafts/${draft.id}`,
          title: draft.title,
          word: null,
          // Also "Updated": a suggestion pass moves a draft's `updated_at` without the
          // student touching it, so "Edited" would sometimes credit them with work Lyra
          // did.
          time: `Updated ${formatRelativeTime(draft.updated_at)}`,
        },
      })
    }
    resume.push(
      ...candidates
        .filter((candidate) => !listed.has(candidate.item.key))
        .sort((a, b) => b.at.localeCompare(a.at))
        .slice(0, 2)
        .map((candidate) => candidate.item),
    )

    return [...waiting, ...broken, ...working, ...due, ...resume].slice(0, CONTINUE_ROWS)
  }, [classId, drafts, attentionCount, ingestingCount, profile?.facts, sessions, solutions, study])

  function ask(text: string, send: boolean) {
    const trimmed = text.trim()
    router.push(
      trimmed.length > 0
        ? chatHandoffUrl(classId, { ask: trimmed, send })
        : chatHandoffUrl(classId),
    )
  }

  function startPractice() {
    // The button is disabled until the document list has loaded, so by the time this can
    // run, a zero really means zero rather than "the query has not answered yet".
    if (readyCount === 0) {
      toast.error('No documents are ready to practice from yet.')
      return
    }
    const studyTitles = [...(study?.decks ?? []), ...(study?.quizzes ?? [])].map(
      (artifact) => artifact.title,
    )
    createQuiz.mutate(
      { title: quickStudyTitle('quiz', studyTitles) },
      {
        onSuccess: (artifact) => router.push(`/classes/${classId}/study/${artifact.id}`),
        onError: (error) =>
          toast.error(
            error instanceof ApiError ? error.message : 'Could not start a practice set.',
          ),
      },
    )
  }

  function startDraft() {
    createDraft.mutate(untitledDraftTitle((drafts ?? []).map((draft) => draft.title)), {
      onSuccess: (artifact) => router.push(`/classes/${classId}/drafts/${artifact.id}`),
      onError: (error) =>
        toast.error(error instanceof ApiError ? error.message : 'Could not start a draft.'),
    })
  }

  return (
    <div className="flex flex-col gap-10">
      {/* The front door: the question goes from here straight into a conversation. The
          class landing asking "what do you want to work on?" and then making the student
          find the composer themselves would be a question it did not mean. */}
      {needsTutorSetup || (documentsLoaded && readyCount === 0) ? (
        <section aria-label="Get started" className="space-y-3">
          <h2 className="font-heading text-xl font-medium">Get your class ready</h2>
          <p className="text-text-secondary text-sm">
            {needsTutorSetup
              ? 'Connect a tutor to ask questions and generate practice. Add course materials for help grounded in your class.'
              : 'Add a syllabus, lecture notes, or a problem set to summarize and practice from.'}
          </p>
          <div className="flex flex-wrap gap-2">
            {needsTutorSetup ? (
              <Button asChild>
                <Link href="/settings">Set up your tutor</Link>
              </Button>
            ) : null}
            <Button variant={needsTutorSetup ? 'outline' : 'default'} asChild>
              <Link href={`/classes/${classId}?tab=files`}>
                <FileUp />
                Add course materials
              </Link>
            </Button>
          </div>
          <p className="text-text-tertiary text-sm">
            You can start writing without files.
            {tutorReady ? ' You can also ask general questions below.' : ''}
          </p>
        </section>
      ) : null}
      {settingsQuery.isError ? (
        <p role="alert" className="text-sm">
          Could not check tutor setup.{' '}
          <Button variant="outline" size="sm" onClick={() => void settingsQuery.refetch()}>
            Retry tutor setup
          </Button>
        </p>
      ) : null}
      {tutorReady ? (
        <section aria-label="Ask Lyra" className="flex flex-col gap-1">
          <form
            onSubmit={(event) => {
              event.preventDefault()
              ask(question, true)
            }}
            className="flex items-center gap-2"
          >
            <Input
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder={
                readyCount > 0 ? `Ask about ${className ?? 'this class'}` : 'Ask Lyra anything'
              }
              aria-label={`Ask about ${className ?? 'this class'}`}
              autoComplete="off"
              className="h-11 flex-1 text-[15px]"
            />
            <Button type="submit" size="lg" className="h-11 shrink-0">
              Ask
            </Button>
          </form>
          {readyCount > 0 && suggestions.length > 0 ? (
            <div className="flex flex-col">
              {suggestions.map((prompt) => (
                <button
                  key={prompt}
                  type="button"
                  onClick={() => ask(prompt, false)}
                  className="group/prompt border-border/70 text-text-secondary hover:text-text-primary focus-visible:ring-ring flex items-baseline justify-between gap-3 border-b py-2.5 text-left text-sm transition-colors duration-150 last:border-b-0 focus-visible:ring-2 focus-visible:outline-none"
                >
                  <span className="min-w-0">{prompt}</span>
                  <span
                    aria-hidden
                    className="text-accent-primary shrink-0 opacity-0 transition-[opacity,transform] duration-150 group-hover/prompt:translate-x-0.5 group-hover/prompt:opacity-100 group-focus-visible/prompt:opacity-100"
                  >
                    →
                  </span>
                </button>
              ))}
            </div>
          ) : null}
        </section>
      ) : null}

      {items.length > 0 ? (
        <section aria-label="Pick up where you left off" className="flex flex-col gap-3">
          <div className="border-border/70 flex items-center gap-2 border-b pb-2">
            <h2 className="text-xs font-medium tracking-[0.14em] uppercase">
              Pick up where you left off
            </h2>
          </div>
          <ul className="flex flex-col gap-1">
            {items.map((item) => (
              <li key={item.key}>
                <Link
                  href={item.href}
                  className="hover:bg-muted focus-visible:ring-ring flex items-center gap-3 rounded-md px-3 py-2 transition-colors focus-visible:ring-2 focus-visible:outline-none"
                >
                  <span className="min-w-0 flex-1 truncate text-sm">{item.title}</span>
                  {item.word ? (
                    <StatusWord tone={item.word.tone} className="shrink-0">
                      {item.word.text}
                    </StatusWord>
                  ) : null}
                  {item.time ? (
                    <span className="text-text-tertiary shrink-0 text-xs">{item.time}</span>
                  ) : null}
                  <ArrowRight aria-hidden className="text-text-tertiary size-3.5 shrink-0" />
                </Link>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section aria-label="Start something" className="flex flex-col gap-3">
        <div className="border-border/70 flex items-center gap-2 border-b pb-2">
          <h2 className="text-xs font-medium tracking-[0.14em] uppercase">Start something</h2>
        </div>
        {/* Practice is the default way in: one balanced session from whatever the class has
            ready, no choosing of decks, quotas, or difficulty first. The rest of the verbs
            stay one click away, one line below. */}
        <div className="flex flex-col gap-3">
          {tutorReady && readyCount > 0 ? (
            <Button
              size="lg"
              className="h-11 self-start"
              onClick={startPractice}
              disabled={createQuiz.isPending || !documentsLoaded}
            >
              {createQuiz.isPending ? <Spinner /> : <Layers aria-hidden className="size-4" />}
              Practice now
            </Button>
          ) : null}
          <div className="flex flex-wrap gap-x-1 gap-y-1">
            {tutorReady && readyCount > 0 ? (
              <Button variant="ghost" size="sm" asChild>
                <Link href={`/classes/${classId}/solutions/new`}>
                  <SquareCheckBig aria-hidden className="size-4" />
                  Solve a problem set
                </Link>
              </Button>
            ) : null}
            <Button variant="ghost" size="sm" onClick={startDraft} disabled={createDraft.isPending}>
              {createDraft.isPending ? <Spinner /> : <PenLine aria-hidden className="size-4" />}
              Start writing
            </Button>
            <Button variant="ghost" size="sm" asChild>
              <Link href={`/classes/${classId}?tab=files`}>
                <FileUp aria-hidden className="size-4" />
                Add documents
              </Link>
            </Button>
          </div>
        </div>
        {/* A failed query is its own state, said out loud: Practice stays held because
            the ready count is unknown, and a held button with no reason on screen reads
            as broken. One line and a retry, not an error dashboard. */}
        {documentsQuery.isError ? (
          <p className="text-text-secondary flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
            <span>The document list did not load, so Practice cannot tell what is ready.</span>
            <Button variant="outline" size="sm" onClick={() => void documentsQuery.refetch()}>
              Retry
            </Button>
          </p>
        ) : null}
        {/* Only a truly empty class is called empty. A class whose uploads all failed or
            are still being read is a different situation, and the continue list above
            already says which. */}
      </section>
    </div>
  )
}
