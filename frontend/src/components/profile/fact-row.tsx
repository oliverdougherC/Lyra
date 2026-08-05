'use client'

import { useState } from 'react'
import { Check, Dot, HelpCircle, X } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'
import type { FactRead } from '@/types'

type FactRowProps = {
  fact: FactRead
  onCorrect: (value: string) => void
  onResolve: (action: 'confirm' | 'reject') => void
  busy: boolean
}

/**
 * Where a fact came from, as evidence rather than as provenance. One document is worth
 * naming; four is worth counting, and the count is the more useful fact of the two, because
 * a topic four uploads agree on is what the course is actually about.
 */
function sourceLine(fact: FactRead): string | null {
  const count = fact.sources.length
  if (count === 0) return null
  if (count === 1) return `From ${fact.sources[0]}`
  return `In ${count} documents`
}

/**
 * Extraction labels come from the model and are frequently filler: a topic labelled
 * `topic`, a value labelled `content`. Those say nothing the section heading has not
 * already said, and printing them exposes the shape of the extraction prompt.
 */
const FILLER_LABELS = new Set([
  'content',
  'value',
  'text',
  'item',
  'fact',
  'detail',
  'name',
  'title',
  'note',
  'deadline',
  'topic',
  'grading',
  'professor',
  'prerequisite',
])

function displayLabel(fact: FactRead): string | null {
  const label = fact.label?.trim()
  if (!label) return null
  const normalized = label.toLowerCase()
  if (normalized === fact.kind || FILLER_LABELS.has(normalized)) return null
  return label
}

export function FactRow({ fact, onCorrect, onResolve, busy }: FactRowProps) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(fact.value)

  const needsConfirmation = !fact.confirmed && fact.confidence === 'low'
  const label = displayLabel(fact)
  const source = sourceLine(fact)

  // A success surface is a claim that the user checked this. Only a confirmed fact earns
  // it: everything else is still something Lyra proposed on its own.
  const confirmation = fact.rejected
    ? {
        label: 'Rejected',
        surfaceClass: 'bg-danger-fill',
        textClass: 'text-danger-foreground',
        iconClass: 'text-danger-text',
      }
    : needsConfirmation
      ? {
          label: 'Needs confirmation',
          surfaceClass: 'bg-info-fill',
          textClass: 'text-info-text',
          iconClass: 'text-info-text',
        }
      : fact.confirmed
        ? {
            label: 'Confirmed',
            surfaceClass: 'bg-success-fill',
            textClass: 'text-success-text',
            iconClass: 'text-success-text',
          }
        : null
  // A check reads as "verified", which only a confirmed fact is. An unconfirmed one gets
  // a neutral bullet: Lyra proposed it, nobody has checked it yet.
  const StateIcon = fact.rejected
    ? X
    : needsConfirmation
      ? HelpCircle
      : fact.confirmed
        ? Check
        : Dot

  function commit() {
    const next = value.trim()
    setEditing(false)
    if (next && next !== fact.value) onCorrect(next)
    else setValue(fact.value)
  }

  return (
    <li
      className={cn(
        'border-b p-3 text-sm last:border-b-0',
        confirmation?.surfaceClass ?? 'bg-card',
      )}
    >
      <div className="flex items-start gap-2">
        <StateIcon
          className={cn('mt-0.5 size-4 shrink-0', confirmation?.iconClass ?? 'text-text-tertiary')}
          aria-hidden
        />

        <div className="min-w-0 flex-1">
          {label || confirmation ? (
            <div className="mb-0.5 flex flex-wrap items-center gap-2">
              {label ? <p className="text-text-secondary text-xs">{label}</p> : null}
              {confirmation ? (
                <span className={cn('text-xs font-medium', confirmation.textClass)}>
                  {confirmation.label}
                </span>
              ) : null}
            </div>
          ) : null}

          {editing ? (
            <Input
              autoFocus
              value={value}
              onChange={(event) => setValue(event.target.value)}
              onBlur={commit}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault()
                  commit()
                } else if (event.key === 'Escape') {
                  event.preventDefault()
                  setValue(fact.value)
                  setEditing(false)
                }
              }}
              className="mt-1"
              aria-label={`Edit ${label || 'this fact'}`}
            />
          ) : (
            <button
              type="button"
              onClick={() => setEditing(true)}
              title="Click to correct this"
              className="focus-visible:ring-ring/50 w-full rounded-sm text-left break-words focus-visible:ring-[3px] focus-visible:outline-none"
            >
              {fact.value}
            </button>
          )}

          {source ? <p className="text-text-tertiary mt-1 text-xs">{source}</p> : null}

          {needsConfirmation ? (
            <>
              <p className="text-text-tertiary mt-1 text-xs">Not used until you confirm this</p>
              <div className="mt-2 flex gap-2">
                <Button size="sm" disabled={busy} onClick={() => onResolve('confirm')}>
                  Confirm
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() => onResolve('reject')}
                >
                  Reject
                </Button>
              </div>
            </>
          ) : null}
        </div>
      </div>
    </li>
  )
}
