'use client'

import { useMemo } from 'react'
import Link from '@/router/link'
import { usePathname, useRouter, useSearchParams } from '@/router/hooks'

import { ClassChatsPanel } from '@/components/classes/class-chats-panel'
import { ClassDraftsPanel } from '@/components/classes/class-drafts-panel'
import { ClassSolutionsPanel } from '@/components/classes/class-solutions-panel'
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty'
import { Skeleton } from '@/components/ui/skeleton'
import { formatRelativeTime } from '@/lib/format'
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
  const pathname = usePathname()
  const router = useRouter()
  const searchParams = useSearchParams()
  const filter = readFilter(searchParams)
  const { data: sessions, isPending: sessionsPending } = useSessions(classId)
  const { data: solutions, isPending: solutionsPending } = useSolutions(classId)
  const { data: drafts, isPending: draftsPending } = useDrafts(classId)
  const { data: study, isPending: studyPending } = useStudyList(classId)

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
    if (sessionsPending || solutionsPending || draftsPending || studyPending) return null
    const rows = [
      ...(sessions ?? []).map((session) => ({
        key: `session-${session.id}`,
        kind: 'Chat' as const,
        title: session.title ?? 'Untitled chat',
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
        status:
          quiz.state === 'ready' &&
          quiz.problems_total !== null &&
          quiz.problems_done < quiz.problems_total
            ? `${quiz.problems_done}/${quiz.problems_total} answered`
            : quiz.state === 'ready'
              ? null
              : stateWord(quiz.state, quiz.stage_detail),
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

  return (
    <div className="flex flex-col gap-4">
      <div
        role="tablist"
        aria-label="Work"
        className="text-text-tertiary inline-flex w-fit items-center gap-1 rounded-md border border-border/70 bg-muted/40 p-1 text-sm"
      >
        {FILTERS.map((item) => (
          <button
            key={item.value}
            role="tab"
            aria-selected={filter === item.value}
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

      {filter === 'all' ? (
        all === null ? (
          <div className="flex flex-col gap-2">
            {Array.from({ length: 4 }).map((_, index) => (
              <Skeleton key={index} className="h-11 w-full rounded-md" />
            ))}
          </div>
        ) : all.length === 0 ? (
          <Empty className="max-w-xl">
            <EmptyHeader>
              <EmptyTitle>Nothing here yet</EmptyTitle>
            </EmptyHeader>
            <EmptyDescription>
              Ask a question, start a problem set, or begin a draft and it will show up here.
            </EmptyDescription>
          </Empty>
        ) : (
          <ul className="flex flex-col gap-1">
            {all.map((row) => (
              <li key={row.key}>
                <Link
                  href={row.href}
                  className="hover:bg-muted focus-visible:ring-ring flex items-center gap-3 rounded-md px-3 py-2 transition-colors focus-visible:ring-2 focus-visible:outline-none"
                >
                  <span className="text-text-tertiary w-16 shrink-0 text-xs">{row.kind}</span>
                  <span className="min-w-0 flex-1 truncate text-sm">{row.title}</span>
                  {row.status ? (
                    <span className="text-text-secondary shrink-0 text-xs">{row.status}</span>
                  ) : null}
                  <span className="text-text-tertiary shrink-0 text-xs">
                    {formatRelativeTime(row.time)}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )
      ) : filter === 'chats' ? (
        <ClassChatsPanel classId={classId} />
      ) : filter === 'solutions' ? (
        <ClassSolutionsPanel classId={classId} />
      ) : (
        <ClassDraftsPanel classId={classId} />
      )}
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
  if (state === 'solving' || state === 'generating') return stageDetail ?? state
  if (state === 'failed') return 'Could not finish'
  return stageDetail ?? state
}
