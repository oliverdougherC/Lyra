'use client'

import Link from '@/router/link'
import { useRef } from 'react'
import { Archive, ArrowRight, MoreVertical, Pencil, Plus, Trash2 } from 'lucide-react'

import { CourseMark } from '@/components/classes/course-mark'
import { Button } from '@/components/ui/button'
import { Reveal } from '@/components/ui/reveal'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { formatCount, formatRelativeTime } from '@/lib/format'
import type { ClassRead } from '@/types'

/** Capped so a large class list does not spend a second cascading in. */
const MAX_STAGGER_STEPS = 5
const STAGGER_SECONDS = 0.05

type ClassCardProps = {
  klass: ClassRead
  index: number
  autoFocus?: boolean
  onRename: (klass: ClassRead) => void
  onDelete: (klass: ClassRead) => void
  onArchive: (klass: ClassRead) => void
}

/**
 * One line of the class ledger. The list is an index page, not a card wall: full-width
 * rows under hairlines, the course name set in the heading face, and the row's metadata
 * kept to the right margin the way a contents page keeps its page numbers.
 */
export function ClassCard({
  klass,
  index,
  autoFocus,
  onRename,
  onDelete,
  onArchive,
}: ClassCardProps) {
  const linkRef = useRef<HTMLAnchorElement>(null)

  const subtitle = [klass.code, klass.semester].filter(Boolean).join(' · ')

  return (
    <Reveal
      once={`class-card-${klass.id}`}
      delay={Math.min(index, MAX_STAGGER_STEPS) * STAGGER_SECONDS}
    >
      <div className="group relative flex items-center gap-4 py-5 pr-1 pl-1 sm:gap-5">
        {/* The link covers the row so the whole line is one target, while the menu button
            sits above it in the stacking order and stays independently clickable. */}
        <Link
          ref={linkRef}
          href={`/classes/${klass.id}`}
          autoFocus={autoFocus}
          className="absolute inset-0 z-0 rounded-md focus-visible:ring-ring/50 focus-visible:ring-[3px] focus-visible:outline-none"
        >
          <span className="sr-only">{klass.name}</span>
        </Link>

        <CourseMark klass={klass} size="lg" className="pointer-events-none relative z-10" />

        <div className="pointer-events-none relative z-10 min-w-0 flex-1">
          <h2 className="font-heading group-hover:text-accent-primary line-clamp-2 text-lg leading-snug font-medium tracking-tight transition-colors duration-150 sm:text-xl">
            {klass.name}
          </h2>
          {subtitle ? (
            <p className="text-text-tertiary mt-0.5 truncate text-sm">{subtitle}</p>
          ) : null}
        </div>

        <div className="pointer-events-none relative z-10 hidden shrink-0 text-right text-xs leading-5 sm:block">
          <p className="text-text-secondary tabular-nums">
            {formatCount(klass.document_count, 'document')}
          </p>
          <p className="text-text-tertiary">{formatRelativeTime(klass.last_active_at)}</p>
        </div>

        <ArrowRight
          aria-hidden
          className="text-accent-primary pointer-events-none relative z-10 size-4 shrink-0 -translate-x-1 opacity-0 transition-[opacity,transform] duration-150 group-focus-within:translate-x-0 group-focus-within:opacity-100 group-hover:translate-x-0 group-hover:opacity-100"
        />

        <div className="relative z-20 shrink-0">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                aria-label={`Actions for ${klass.name}`}
                className="size-8 opacity-0 transition-opacity group-focus-within:opacity-100 group-hover:opacity-100 focus-visible:opacity-100 data-[state=open]:opacity-100"
              >
                <MoreVertical />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={() => onRename(klass)}>
                <Pencil />
                Rename
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={() => onArchive(klass)}>
                <Archive />
                Archive
              </DropdownMenuItem>
              <DropdownMenuItem variant="destructive" onSelect={() => onDelete(klass)}>
                <Trash2 />
                Delete
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
    </Reveal>
  )
}

/**
 * Closes the ledger with the one action the page is for: a final quiet line, not a dashed
 * placeholder tile pretending to be content.
 */
export function NewClassCard({ onClick }: { onClick: (trigger: HTMLButtonElement) => void }) {
  return (
    <button
      type="button"
      onClick={(event) => onClick(event.currentTarget)}
      className="group/new text-text-secondary hover:text-accent-primary focus-visible:ring-ring/50 flex w-full items-center gap-4 py-5 pr-1 pl-1 text-left text-sm font-medium transition-colors duration-150 focus-visible:ring-[3px] focus-visible:outline-none sm:gap-5"
    >
      <span className="border-border-strong text-text-tertiary group-hover/new:border-accent-primary group-hover/new:text-accent-primary flex size-12 shrink-0 items-center justify-center rounded-md border border-dashed transition-colors duration-150">
        <Plus className="size-5" aria-hidden />
      </span>
      New class
    </button>
  )
}

/** Mirrors ClassCard's box model exactly, so data arriving causes no layout shift. */
export function ClassCardSkeleton() {
  return (
    <div className="flex items-center gap-4 py-5 pr-1 pl-1 sm:gap-5" aria-hidden>
      <div className="bg-muted size-12 shrink-0 motion-safe:animate-pulse rounded-md" />
      <div className="min-w-0 flex-1 space-y-2">
        <div className="bg-muted h-5 w-2/5 motion-safe:animate-pulse rounded" />
        <div className="bg-muted h-3 w-1/4 motion-safe:animate-pulse rounded" />
      </div>
      <div className="hidden shrink-0 space-y-2 sm:block">
        <div className="bg-muted ml-auto h-3 w-20 motion-safe:animate-pulse rounded" />
        <div className="bg-muted ml-auto h-3 w-14 motion-safe:animate-pulse rounded" />
      </div>
    </div>
  )
}
