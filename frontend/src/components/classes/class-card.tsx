'use client'

import Link from 'next/link'
import { useRef } from 'react'
import { MoreVertical, Pencil, Trash2 } from 'lucide-react'

import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Reveal } from '@/components/ui/reveal'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { formatCount, formatRelativeTime, initialsFor } from '@/lib/format'
import type { ClassRead } from '@/types'

/** Capped so a large class list does not spend a second cascading in. */
const MAX_STAGGER_STEPS = 5
const STAGGER_SECONDS = 0.05

const COURSE_TONES = [
  'bg-accent-surface text-accent-surface-foreground',
  'bg-accent-secondary text-accent-secondary-foreground',
  'bg-accent-tertiary text-accent-tertiary-foreground',
] as const

type ClassCardProps = {
  klass: ClassRead
  index: number
  autoFocus?: boolean
  onRename: (klass: ClassRead) => void
  onDelete: (klass: ClassRead) => void
}

export function ClassCard({ klass, index, autoFocus, onRename, onDelete }: ClassCardProps) {
  const linkRef = useRef<HTMLAnchorElement>(null)

  const subtitle = [klass.code, klass.semester].filter(Boolean).join(' · ')
  const courseTone = COURSE_TONES[klass.id % COURSE_TONES.length]

  return (
    <Reveal delay={Math.min(index, MAX_STAGGER_STEPS) * STAGGER_SECONDS}>
      <Card className="group relative h-full gap-0 p-5 transition-shadow duration-200 hover:shadow-md">
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

        <div className="pointer-events-none relative z-10 flex items-start gap-3">
          <Avatar className="size-10 rounded-md">
            <AvatarFallback className={`${courseTone} rounded-md text-sm font-semibold`}>
              {initialsFor(klass.name, klass.code)}
            </AvatarFallback>
          </Avatar>
          <div className="min-w-0 flex-1">
            <h2 className="truncate text-base leading-6 font-medium">{klass.name}</h2>
            {subtitle ? <p className="text-muted-foreground truncate text-sm">{subtitle}</p> : null}
          </div>
        </div>

        <div className="text-text-tertiary relative z-10 mt-6 flex items-center gap-2 text-xs">
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
                className="size-8 opacity-0 transition-opacity group-focus-within:opacity-100 group-hover:opacity-100 focus-visible:opacity-100 data-[state=open]:opacity-100"
              >
                <MoreVertical />
                <span className="sr-only">Actions for {klass.name}</span>
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={() => onRename(klass)}>
                <Pencil />
                Rename
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

/** Mirrors ClassCard's box model exactly, so data arriving causes no layout shift. */
export function ClassCardSkeleton() {
  return (
    <Card className="h-full gap-0 p-5 shadow-sm" aria-hidden>
      <div className="flex items-start gap-3">
        <div className="bg-muted size-10 motion-safe:animate-pulse rounded-md" />
        <div className="min-w-0 flex-1 space-y-2 py-1">
          <div className="bg-muted h-4 w-3/4 motion-safe:animate-pulse rounded" />
          <div className="bg-muted h-3 w-1/2 motion-safe:animate-pulse rounded" />
        </div>
      </div>
      <div className="mt-6 flex h-4 items-center">
        <div className="bg-muted h-3 w-2/5 motion-safe:animate-pulse rounded" />
      </div>
    </Card>
  )
}
