'use client'

import { ArrowRight } from 'lucide-react'
import Link from 'next/link'

import { CourseMark } from '@/components/classes/course-mark'
import { StatusWord } from '@/components/ex-libris'
import { Skeleton } from '@/components/ui/skeleton'
import { formatRelativeTime, formatSessionFallbackTitle, parseTimestamp } from '@/lib/format'
import { useSessions } from '@/lib/hooks/use-chat'
import { useDrafts } from '@/lib/hooks/use-drafts'
import { useSolutions } from '@/lib/hooks/use-solutions'
import { useStudyList } from '@/lib/hooks/use-study'
import type { ClassRead } from '@/types'

type ResumeTarget = {
  href: string
  title: string
  /** What the row is, or needs, in words. */
  note: string
  noteTone: 'nominal' | 'info'
}

/**
 * The way back into yesterday's work, on the page the app opens on.
 *
 * One row, for the most recently active class only: the class list already says when each
 * class was last touched, but not what was happening there, so a returning student's first
 * click used to be archaeology. Anything waiting on the student outranks anything merely
 * recent. One class, not all of them, because home is an index, not a dashboard.
 */
export function ResumeStrip({ klass }: { klass: ClassRead }) {
  const { data: sessions, isPending: sessionsPending } = useSessions(klass.id)
  const { data: solutions, isPending: solutionsPending } = useSolutions(klass.id)
  const { data: drafts, isPending: draftsPending } = useDrafts(klass.id)
  const { data: study, isPending: studyPending } = useStudyList(klass.id)

  const pending = sessionsPending || solutionsPending || draftsPending || studyPending

  let target: ResumeTarget | null = null

  const waiting = (solutions ?? []).find((solution) => solution.state === 'awaiting_review')
  const dueDeck = (study?.decks ?? []).find((deck) => deck.state === 'ready' && deck.due_count > 0)

  if (waiting) {
    target = {
      href: `/classes/${klass.id}/solutions/${waiting.id}`,
      title: waiting.title,
      note: 'Waiting for your check',
      noteTone: 'info',
    }
  } else if (dueDeck) {
    target = {
      href: `/classes/${klass.id}/study/${dueDeck.id}`,
      title: dueDeck.title,
      note: `${dueDeck.due_count} cards due`,
      noteTone: 'info',
    }
  } else {
    // The most recent destination of any kind, conversations included.
    const candidates: { at: number; target: ResumeTarget }[] = []
    const newestSession =
      sessions && sessions.length > 0 ? [...sessions].sort((a, b) => b.id - a.id)[0] : null
    if (newestSession) {
      candidates.push({
        at: parseTimestamp(newestSession.created_at).getTime(),
        target: {
          href: `/classes/${klass.id}/chat?session=${newestSession.id}`,
          title: newestSession.title || formatSessionFallbackTitle(newestSession.created_at),
          note: formatRelativeTime(newestSession.created_at),
          noteTone: 'nominal',
        },
      })
    }
    for (const artifact of [
      ...(solutions ?? []).map((item) => ({ item, href: 'solutions' })),
      ...(drafts ?? []).map((item) => ({ item, href: 'drafts' })),
      ...(study?.decks ?? []).map((item) => ({ item, href: 'study' })),
      ...(study?.quizzes ?? []).map((item) => ({ item, href: 'study' })),
    ]) {
      candidates.push({
        at: parseTimestamp(artifact.item.updated_at).getTime(),
        target: {
          href: `/classes/${klass.id}/${artifact.href}/${artifact.item.id}`,
          title: artifact.item.title,
          note: formatRelativeTime(artifact.item.updated_at),
          noteTone: 'nominal',
        },
      })
    }
    target = candidates.sort((a, b) => b.at - a.at)[0]?.target ?? null
  }

  if (pending) {
    return (
      <section aria-label="Pick up where you left off" aria-busy="true">
        <Skeleton className="h-14 w-full rounded-md" />
      </section>
    )
  }

  if (!target) return null

  return (
    <section aria-label="Pick up where you left off">
      <Link
        href={target.href}
        className="group border-border bg-card hover:border-border-strong focus-visible:ring-ring flex items-center gap-3 rounded-md border px-3 py-2.5 transition-colors focus-visible:ring-2 focus-visible:outline-none"
      >
        <CourseMark klass={klass} size="sm" className="shrink-0" />
        <span className="min-w-0 flex-1">
          <span className="text-text-tertiary block text-xs">Pick up where you left off</span>
          <span className="text-text-primary block truncate text-sm font-medium">
            {target.title}
          </span>
        </span>
        <StatusWord tone={target.noteTone} className="shrink-0">
          {target.note}
        </StatusWord>
        <ArrowRight
          aria-hidden
          className="text-accent-primary size-4 shrink-0 -translate-x-1 opacity-0 transition-[opacity,transform] duration-150 group-focus-visible:translate-x-0 group-focus-visible:opacity-100 group-hover:translate-x-0 group-hover:opacity-100"
        />
      </Link>
    </section>
  )
}
