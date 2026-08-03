'use client'

import Link from 'next/link'
import { useRef } from 'react'
import { Archive, MoreVertical, Pencil, Plus, Trash2 } from 'lucide-react'

import { CourseMark } from '@/components/classes/course-mark'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
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
      className="h-full"
    >
      <Card className="group relative flex h-full flex-col gap-0 p-5 transition-shadow duration-200 hover:shadow-md">
        {/* The link covers the card so the whole surface is one target, while the menu
            button sits above it in the stacking order and stays independently clickable. */}
        <Link
          ref={linkRef}
          href={`/classes/${klass.id}`}
          autoFocus={autoFocus}
          className="absolute inset-0 z-0 rounded-[inherit] focus-visible:ring-ring/50 focus-visible:ring-[3px] focus-visible:outline-none"
        >
          <span className="sr-only">{klass.name}</span>
        </Link>

        <div className="pointer-events-none relative z-10 flex items-start gap-3 pr-8">
          <CourseMark klass={klass} />
          <div className="min-w-0 flex-1">
            {/* Wraps to two lines rather than truncating: a course name is how the user
                tells one card from another, and `Continuous-Time Sign...` does not. */}
            <h2 className="line-clamp-2 text-base leading-6 font-medium">{klass.name}</h2>
            {subtitle ? <p className="text-muted-foreground truncate text-sm">{subtitle}</p> : null}
          </div>
        </div>

        {/* Pushed to the card's floor rather than sitting under a fixed margin, so cards
            in a row square up their footers however long their titles run. */}
        <div className="text-text-tertiary relative z-10 mt-auto flex items-center gap-2 pt-6 text-xs">
          <span>{formatCount(klass.document_count, 'document')}</span>
          <span aria-hidden>·</span>
          <span>{formatRelativeTime(klass.last_active_at)}</span>
        </div>

        <div className="absolute top-3 right-3 z-20">
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
      </Card>
    </Reveal>
  )
}

/**
 * Closes the grid with the one action the screen is for. A row of cards trailing off into
 * empty canvas reads as an unfinished page; a dashed tile reads as an invitation, and it
 * puts New class within reach of wherever the eye already is.
 */
export function NewClassCard({ onClick }: { onClick: (trigger: HTMLButtonElement) => void }) {
  return (
    <button
      type="button"
      onClick={(event) => onClick(event.currentTarget)}
      className="text-text-secondary hover:border-accent-primary hover:text-accent-primary focus-visible:ring-ring/50 flex h-full min-h-[122px] flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border-strong text-sm transition-colors duration-200 focus-visible:ring-[3px] focus-visible:outline-none"
    >
      <Plus className="size-5" aria-hidden />
      New class
    </button>
  )
}

/** Mirrors ClassCard's box model exactly, so data arriving causes no layout shift. */
export function ClassCardSkeleton() {
  return (
    <Card className="flex h-full flex-col gap-0 p-5 shadow-sm" aria-hidden>
      <div className="flex items-start gap-3">
        <div className="bg-muted size-10 motion-safe:animate-pulse rounded-md" />
        <div className="min-w-0 flex-1 space-y-2 py-1">
          <div className="bg-muted h-4 w-3/4 motion-safe:animate-pulse rounded" />
          <div className="bg-muted h-3 w-1/2 motion-safe:animate-pulse rounded" />
        </div>
      </div>
      <div className="mt-auto flex h-4 items-center pt-6">
        <div className="bg-muted h-3 w-2/5 motion-safe:animate-pulse rounded" />
      </div>
    </Card>
  )
}
