'use client'

import { useQueryClient } from '@tanstack/react-query'
import { Check, X } from 'lucide-react'
import { toast } from 'sonner'

import { MathText } from '@/components/solutions/math-text'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api'
import { draftKeys, useAcceptEdit, useRejectEdit } from '@/lib/hooks/use-drafts'
import { cn } from '@/lib/utils'
import type { AcceptRejectResult, Hunk, PendingEdit } from '@/types'

type SuggestionPanelProps = {
  draftId: number
  edit: PendingEdit
  /** The draft body as it stands, which is what a stale edit is compared against. */
  currentBody: string
  /**
   * Every accept or reject answers how much of the suggestion is left. The workspace
   * refetches the body after an accept and closes the panel when nothing remains.
   */
  onApplied: (result: AcceptRejectResult) => void
}

/**
 * A whole-document suggestion, reviewed hunk by hunk in the right rail.
 *
 * The edit arrives as unified-diff hunks and leaves as `{index, hash}` echoes: the hash
 * is the race guard, and a 409 means the hunk set moved under the panel, so the server's
 * message is toasted and the edit refetched. A stale edit can no longer anchor its hunks
 * at all, so it swaps to a side-by-side reading with the two decisions that remain:
 * reject it, or replace the document with the proposal.
 */
export function SuggestionPanel({ draftId, edit, currentBody, onApplied }: SuggestionPanelProps) {
  const queryClient = useQueryClient()
  const accept = useAcceptEdit(draftId)
  const reject = useRejectEdit(draftId)
  const busy = accept.isPending || reject.isPending

  function onConflict(error: unknown) {
    // A 409 is the server saying the edit moved: what it says is the useful part, and the
    // refetch is what brings the panel back onto the truth.
    toast.error(error instanceof ApiError ? error.message : 'The suggestion changed.')
    queryClient.invalidateQueries({ queryKey: draftKeys.pending(draftId) })
    queryClient.invalidateQueries({ queryKey: draftKeys.detail(draftId) })
  }

  function acceptHunk(hunk: Hunk) {
    accept.mutate(
      { editId: edit.id, hunk: { index: hunk.index, hash: hunk.hash } },
      { onSuccess: onApplied, onError: onConflict },
    )
  }

  function rejectHunk(hunk: Hunk) {
    reject.mutate(
      { editId: edit.id, hunk: { index: hunk.index, hash: hunk.hash } },
      { onSuccess: onApplied, onError: onConflict },
    )
  }

  function acceptAll(force = false) {
    accept.mutate({ editId: edit.id, force }, { onSuccess: onApplied, onError: onConflict })
  }

  function rejectAll() {
    reject.mutate({ editId: edit.id }, { onSuccess: onApplied, onError: onConflict })
  }

  if (edit.stale) {
    return (
      <div className="flex flex-col gap-4">
        <PanelHeader
          title={edit.note ?? 'Suggested changes'}
          // A stale edit's hunks no longer anchor, so piecemeal review is gone.
          actions={null}
        />
        <p className="text-text-secondary text-sm">
          The draft changed after Lyra wrote this, so the pieces no longer line up with the
          document. Read the two versions side by side: keep the document as it is, or
          replace it with the proposal.
        </p>
        <div className="flex flex-col gap-3">
          <section aria-label="Current document">
            <h3 className="text-text-tertiary mb-1.5 text-xs font-medium tracking-[0.14em] uppercase">
              Current
            </h3>
            <div className="border-border bg-card max-h-64 overflow-y-auto rounded-md border p-3">
              <MathText className="text-text-secondary text-sm">{currentBody}</MathText>
            </div>
          </section>
          <section aria-label="Proposed document">
            <h3 className="text-text-tertiary mb-1.5 text-xs font-medium tracking-[0.14em] uppercase">
              Proposed
            </h3>
            <div className="border-border bg-card max-h-64 overflow-y-auto rounded-md border p-3">
              <MathText className="text-text-secondary text-sm">{edit.proposed_content}</MathText>
            </div>
          </section>
        </div>
        <div className="flex gap-2">
          <Button size="sm" disabled={busy} onClick={() => acceptAll(true)}>
            <Check className="size-4" />
            Replace document
          </Button>
          <Button variant="outline" size="sm" disabled={busy} onClick={rejectAll}>
            Reject
          </Button>
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <PanelHeader
        title={edit.note ?? 'Suggested changes'}
        actions={
          <div className="flex gap-2">
            <Button size="sm" disabled={busy} onClick={() => acceptAll()}>
              Accept all
            </Button>
            <Button variant="outline" size="sm" disabled={busy} onClick={rejectAll}>
              Reject all
            </Button>
          </div>
        }
      />
      <ul className="flex flex-col gap-3" aria-label="Suggested changes, hunk by hunk">
        {edit.hunks.map((hunk) => (
          <li key={hunk.hash}>
            <HunkCard
              hunk={hunk}
              busy={busy}
              onAccept={() => acceptHunk(hunk)}
              onReject={() => rejectHunk(hunk)}
            />
          </li>
        ))}
      </ul>
    </div>
  )
}

/** The instruction as the panel's name, with the whole-edit decisions beside it. */
function PanelHeader({ title, actions }: { title: string; actions: React.ReactNode }) {
  return (
    <header className="flex flex-col gap-3">
      <h2 className="text-text-primary text-sm font-medium text-pretty">{title}</h2>
      {actions}
    </header>
  )
}

/**
 * One hunk as a small card: context lines quiet, removals on the danger pair, additions
 * on the success pair, and the two per-hunk decisions echoing `{index, hash}`.
 */
function HunkCard({
  hunk,
  busy,
  onAccept,
  onReject,
}: {
  hunk: Hunk
  busy: boolean
  onAccept: () => void
  onReject: () => void
}) {
  return (
    <article className="border-border bg-card overflow-hidden rounded-md border">
      <div className="font-mono text-xs leading-relaxed">
        {hunk.lines.map((line, index) => {
          const sign = line.charAt(0)
          const text = line.slice(1)
          return (
            <div
              key={index}
              className={cn(
                'flex gap-2 px-2',
                sign === '-' && 'bg-danger-fill text-danger-text',
                sign === '+' && 'bg-success-fill text-success-text',
                sign === ' ' && 'text-text-tertiary',
              )}
            >
              <span aria-hidden className="w-3 shrink-0 text-center select-none">
                {sign === ' ' ? '' : sign}
              </span>
              <span className="break-words whitespace-pre-wrap">{text}</span>
            </div>
          )
        })}
      </div>
      <footer className="border-border flex gap-1 border-t px-2 py-1.5">
        <Button
          variant="ghost"
          size="icon"
          className="size-7"
          aria-label={`Accept change ${hunk.index + 1}`}
          disabled={busy}
          onClick={onAccept}
        >
          <Check className="size-3.5" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="size-7"
          aria-label={`Reject change ${hunk.index + 1}`}
          disabled={busy}
          onClick={onReject}
        >
          <X className="size-3.5" />
        </Button>
      </footer>
    </article>
  )
}
