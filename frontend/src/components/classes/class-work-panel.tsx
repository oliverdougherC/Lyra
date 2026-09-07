'use client'

import { useMemo, useState } from 'react'
import Link from '@/router/link'
import { usePathname, useRouter, useSearchParams } from '@/router/hooks'

import { ClassChatsPanel } from '@/components/classes/class-chats-panel'
import { ClassDraftsPanel } from '@/components/classes/class-drafts-panel'
import { ClassSolutionsPanel } from '@/components/classes/class-solutions-panel'
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { formatRelativeTime, formatSessionFallbackTitle } from '@/lib/format'
import { cn } from '@/lib/utils'
import { useDrafts } from '@/lib/hooks/use-drafts'
import { useSolutions } from '@/lib/hooks/use-solutions'
import { useSessions } from '@/lib/hooks/use-chat'
import { useStudyList } from '@/lib/hooks/use-study'

type WorkFilter = 'all' | 'chats' | 'solutions' | 'drafts'

function readFilter(searchParams: URLSearchParams): WorkFilter {
  const explicit = searchParams.get('work')
  if (explicit === 'chats' || explicit === 'solutions' || explicit === 'drafts') return explicit
  if (explicit !== null) return 'all'
  // A legacy ?tab=chats/solutions/drafts URL arrives here a frame before the class page
  // rewrites it to ?tab=work&work=...: read the filter the old tab implied, so the list
  // never flashes the unfiltered view first.
  const tab = searchParams.get('tab')
  return tab === 'chats' || tab === 'solutions' || tab === 'drafts' ? tab : 'all'
}

/**
 * Everything the student was doing in a class, in one list, so no one has to remember which
 * tab a conversation, a problem set, a flashcard deck, or a half-written answer lived under.
 *
 * The "all" view is the getting-back-to-it list: one row per thing, most recent first.
 * Decks and quizzes appear here as things to get back to; Practice is still their home for
 * starting and doing them, and the filters hand each kind over to the panel that manages
 * it - rename, delete, and create live there, not here, so the list stays a list.
 */
export function ClassWorkPanel({ classId }: { classId: number }) {
  return <WorkList key={classId} classId={classId} />
}

function WorkList({ classId }: { classId: number }) {
  const storageKey = `lyra:class:${classId}:work-list`
  const [view, setView] = useState(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey) ?? '{}')
      return {
        query: typeof saved.query === 'string' ? saved.query : '',
        limit: Number.isInteger(saved.limit) && saved.limit >= 20 ? saved.limit : 20,
      }
    } catch {
      return { query: '', limit: 20 }
    }
  })
  function updateView(next: typeof view) {
    setView(next)
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(next))
    } catch {
      /* Browsing still works without storage. */
    }
  }
  const pathname = usePathname()
  const router = useRouter()
  const searchParams = useSearchParams()
  const filter = readFilter(searchParams)
  const sessionsQuery = useSessions(classId)
  const { data: sessions, isPending: sessionsPending } = sessionsQuery
  const solutionsQuery = useSolutions(classId)
  const { data: solutions, isPending: solutionsPending } = solutionsQuery
  const draftsQuery = useDrafts(classId)
  const { data: drafts, isPending: draftsPending } = draftsQuery
  const studyQuery = useStudyList(classId)
  const { data: study, isPending: studyPending } = studyQuery

  const [retryNames, setRetryNames] = useState<string[]>([])
  const unavailable = [
    { name: 'chats', query: sessionsQuery },
    { name: 'solutions', query: solutionsQuery },
    { name: 'drafts', query: draftsQuery },
    { name: 'practice', query: studyQuery },
  ].filter(({ name, query }) => query.isError || retryNames.includes(name))

  // The filter is part of the route, so it lives in the hash like the tab does: a link to
  // the class's work with a filter applied is a link that reloads and backs forward as
  // expected. `replace` rather than `push`: stepping back out of the Work tab should leave
  // the tab, not walk through every filter in reverse.
  const setFilter = (next: WorkFilter) => {
    const params = new URLSearchParams(searchParams.toString())
    if (next === 'all') params.delete('work')
    else params.set('work', next)
    const query = params.toString()
    router.replace(`${pathname}${query ? `?${query}` : ''}`, { scroll: false })
  }

  const all = useMemo(() => {
    if (sessionsPending && solutionsPending && draftsPending && studyPending) return null
    const rows = [
      ...(sessions ?? []).map((session) => ({
        key: `session-${session.id}`,
        kind: 'Chat' as const,
        title: session.title || formatSessionFallbackTitle(session.created_at),
        href: `/classes/${classId}/chat?session=${session.id}`,
        status: null,
        time: session.created_at,
      })),
      ...(solutions ?? []).map((solution) => ({
        key: `solution-${solution.id}`,
        kind: 'Solution' as const,
        title: solution.title,
        href: `/classes/${classId}/solutions/${solution.id}`,
        status:
          solution.state === 'ready' ? null : stateWord(solution.state, solution.stage_detail),
        time: solution.updated_at,
      })),
      ...(drafts ?? []).map((draft) => ({
        key: `draft-${draft.id}`,
        kind: 'Draft' as const,
        title: draft.title,
        href: `/classes/${classId}/drafts/${draft.id}`,
        status: draft.state === 'ready' ? null : stateWord(draft.state, draft.stage_detail),
        time: draft.updated_at,
      })),
      ...(study?.decks ?? []).map((deck) => ({
        key: `deck-${deck.id}`,
        kind: 'Deck' as const,
        title: deck.title,
        href: `/classes/${classId}/study/${deck.id}`,
        status:
          deck.state === 'ready'
            ? deck.due_count > 0
              ? `${deck.due_count} cards due`
              : null
            : stateWord(deck.state, deck.stage_detail),
        time: deck.updated_at,
      })),
      ...(study?.quizzes ?? []).map((quiz) => ({
        key: `quiz-${quiz.id}`,
        kind: 'Quiz' as const,
        title: quiz.title,
        href: `/classes/${classId}/study/${quiz.id}`,
        status: quiz.state === 'ready' ? null : stateWord(quiz.state, quiz.stage_detail),
        time: quiz.updated_at,
      })),
    ]
    rows.sort((a, b) => new Date(b.time).getTime() - new Date(a.time).getTime())
    return rows
  }, [
    classId,
    drafts,
    draftsPending,
    sessions,
    sessionsPending,
    solutions,
    solutionsPending,
    study,
    studyPending,
  ])

  const kinds = { chats: 'Chat', solutions: 'Solution', drafts: 'Draft' }
  const filtered = all?.filter((row) => filter === 'all' || row.kind === kinds[filter]) ?? []
  const matches = filtered.filter((row) =>
    row.title.toLocaleLowerCase().includes(view.query.trim().toLocaleLowerCase()),
  )
  const retrying = retryNames.length > 0 || unavailable.some(({ query }) => query.isFetching)

  return (
    <div className="flex flex-col gap-4">
      <div
        role="group"
        aria-label="Work"
        className="text-text-tertiary inline-flex w-fit items-center gap-1 rounded-md border border-border/70 bg-muted/40 p-1 text-sm"
      >
        {FILTERS.map((item) => (
          <button
            key={item.value}
            aria-pressed={filter === item.value}
            type="button"
            onClick={() => setFilter(item.value)}
            className={cn(
              'rounded-sm px-2.5 py-1 transition-colors duration-150',
              'focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
              filter === item.value
                ? 'bg-background text-text-primary shadow-sm'
                : 'text-text-secondary hover:text-text-primary',
            )}
          >
            {item.label}
          </button>
        ))}
      </div>

      {unavailable.length > 0 ? (
        <Alert variant="destructive">
          <AlertTitle>Some work could not be refreshed</AlertTitle>
          <AlertDescription>
            <p>
              Could not load {unavailable.map(({ name }) => name).join(', ')}. Previously loaded
              items remain available.
            </p>
            <Button
              variant="outline"
              size="sm"
              disabled={retrying}
              onClick={() => {
                setRetryNames(unavailable.map(({ name }) => name))
                void Promise.all(unavailable.map(({ query }) => query.refetch())).finally(() =>
                  setRetryNames([]),
                )
              }}
            >
              {retrying ? 'Retrying…' : 'Retry all'}
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}

      {filtered.length >= 10 || view.query ? (
        <div className="flex items-center gap-2">
          <Input
            type="search"
            aria-label="Search work by title"
            placeholder="Search work"
            value={view.query}
            onChange={(event) => updateView({ query: event.target.value, limit: 20 })}
          />
          {view.query ? (
            <Button variant="ghost" onClick={() => updateView({ query: '', limit: 20 })}>
              Clear search
            </Button>
          ) : null}
        </div>
      ) : null}

      {filter === 'all' ? (
        all === null ? (
          <div className="flex flex-col gap-2">
            {Array.from({ length: 4 }).map((_, index) => (
              <Skeleton key={index} className="h-11 w-full rounded-md" />
            ))}
          </div>
        ) : all.length === 0 && unavailable.length > 0 ? null : all.length === 0 ? (
          <Empty className="max-w-xl">
            <EmptyHeader>
              <EmptyTitle>Nothing here yet</EmptyTitle>
            </EmptyHeader>
            <EmptyDescription>
              Ask a question, start a problem set, or begin a draft and it will show up here.
            </EmptyDescription>
          </Empty>
        ) : matches.length === 0 ? (
          <p role="status" className="text-text-secondary text-sm">
            No work matches this search.
          </p>
        ) : (
          <>
            <ul className="flex flex-col gap-1">
              {matches.slice(0, view.limit).map((row) => (
                <li key={row.key}>
                  <Link
                    href={row.href}
                    className="hover:bg-muted focus-visible:ring-ring flex min-w-0 flex-col gap-1 rounded-md px-3 py-2 transition-colors focus-visible:ring-2 focus-visible:outline-none"
                  >
                    <span className="min-w-0 break-words text-base">{row.title}</span>
                    <span className="text-text-tertiary flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
                      <span>{row.kind}</span>
                      {row.status ? (
                        <span className="text-text-secondary">{row.status}</span>
                      ) : null}
                      <span>{formatRelativeTime(row.time)}</span>
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          </>
        )
      ) : filter === 'chats' ? (
        <ClassChatsPanel classId={classId} query={view.query} limit={view.limit} managedRecovery />
      ) : filter === 'solutions' ? (
        <ClassSolutionsPanel
          classId={classId}
          query={view.query}
          limit={view.limit}
          managedRecovery
        />
      ) : (
        <ClassDraftsPanel classId={classId} query={view.query} limit={view.limit} managedRecovery />
      )}
      {matches.length > view.limit ? (
        <Button variant="outline" onClick={() => updateView({ ...view, limit: view.limit + 20 })}>
          Show more work ({matches.length - view.limit} remaining)
        </Button>
      ) : null}
    </div>
  )
}

const FILTERS: { value: WorkFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'chats', label: 'Chats' },
  { value: 'solutions', label: 'Solutions' },
  { value: 'drafts', label: 'Drafts' },
]

/** A non-ready run, in words: the stage line if there is one, else the state. */
function stateWord(state: string, stageDetail: string | null): string {
  if (state === 'awaiting_review') return 'Review problems'
  if (state === 'failed') return 'Could not finish'
  if (state === 'pending') return 'Queued'
  if (state === 'segmenting') return 'Reading problems'
  if (state === 'solving') return stageDetail ?? 'Solving problems'
  if (state === 'generating') return stageDetail ?? 'Creating'
  if (state === 'cancelled') return 'Stopped'
  return stageDetail ?? 'In progress'
}
