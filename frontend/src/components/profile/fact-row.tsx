'use client'

import { useState } from 'react'
import { Check, HelpCircle, X } from 'lucide-react'

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

export function FactRow({ fact, onCorrect, onResolve, busy }: FactRowProps) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(fact.value)

  const needsConfirmation = !fact.confirmed && fact.confidence === 'low'

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
  const StateIcon = fact.rejected ? X : needsConfirmation ? HelpCircle : Check

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
          className={cn('mt-0.5 size-4 shrink-0', confirmation?.iconClass ?? 'text-success-text')}
          aria-hidden
        />

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            {fact.label ? <p className="text-text-secondary text-xs">{fact.label}</p> : null}
            {confirmation ? (
              <span className={cn('text-xs font-medium', confirmation.textClass)}>
                {confirmation.label}
              </span>
            ) : null}
          </div>

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
              aria-label={`Edit ${fact.label || 'fact'}`}
            />
          ) : (
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="focus-visible:ring-ring/50 w-full rounded-sm text-left break-words focus-visible:ring-[3px] focus-visible:outline-none"
            >
              {fact.value}
            </button>
          )}

          {fact.source_filename ? (
            <p className="text-text-tertiary mt-1 text-xs">From {fact.source_filename}</p>
          ) : null}

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
