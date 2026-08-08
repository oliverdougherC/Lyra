'use client'

import { Check, CornerDownRight, RotateCcw, Sparkles } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import { MathText } from '@/components/solutions/math-text'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import {
  useComments,
  useReplyToComment,
  useResolveComment,
  useStartPass,
} from '@/lib/hooks/use-drafts'
import { cn } from '@/lib/utils'
import type { CommentSeverity, DraftComment } from '@/types'

/**
 * The reviewer's findings, read in severity order: what would sink the piece first,
 * suggestions last, and within a severity in the order the document reads. Findings
 * whose passage has since been deleted keep their place in a group of their own at the
 * bottom - the finding survives its anchor - and resolved threads dim out below that.
 *
 * Clicking a thread's quote jumps the editor to its underline. Replying and resolving
 * live inline: the reply is the student's side of the conversation the writer joins
 * through its own tool, and resolution is the student's gesture alone.
 */
export function CommentList({
  draftId,
  onJump,
}: {
  draftId: number
  /** Scroll the editor to this thread's anchor. False when it has none to jump to. */
  onJump?: (comment: DraftComment) => boolean
}) {
  const { data: threads, isPending } = useComments(draftId)

  if (isPending) {
    return (
      <div className="flex flex-col gap-2" aria-busy="true">
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-16 w-full" />
      </div>
    )
  }
  if (!threads || threads.length === 0) {
    return (
      <p className="text-text-tertiary text-sm">
        No comments yet. Ask Lyra to review the draft, and its findings land here, anchored to the
        passages they are about.
      </p>
    )
  }

  const open = threads.filter((thread) => !thread.resolved && !thread.orphaned)
  const orphaned = threads.filter((thread) => !thread.resolved && thread.orphaned === 1)
  const resolved = threads.filter((thread) => thread.resolved === 1)

  return (
    <div className="flex flex-col gap-4">
      {open.length > 0 ? (
        <ul className="flex flex-col gap-2" aria-label="Open comments">
          {sortBySeverity(open).map((thread) => (
            <CommentThread key={thread.id} draftId={draftId} thread={thread} onJump={onJump} />
          ))}
        </ul>
      ) : (
        <p className="text-text-tertiary text-sm">Nothing open.</p>
      )}

      {orphaned.length > 0 ? (
        <section aria-label="Comments on passages that are gone">
          <p className="eyebrow text-text-tertiary mb-2">No longer anchored</p>
          <ul className="flex flex-col gap-2">
            {sortBySeverity(orphaned).map((thread) => (
              <CommentThread key={thread.id} draftId={draftId} thread={thread} />
            ))}
          </ul>
        </section>
      ) : null}

      {resolved.length > 0 ? (
        <section aria-label="Resolved comments">
          <p className="eyebrow text-text-tertiary mb-2">Resolved</p>
          <ul className="flex flex-col gap-2 opacity-60">
            {resolved.map((thread) => (
              <CommentThread key={thread.id} draftId={draftId} thread={thread} />
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  )
}

/**
 * Severity first, then reading order. A whole-document finding has no anchor and leads
 * its severity: it is the broadest thing said at that level.
 */
function sortBySeverity(threads: DraftComment[]): DraftComment[] {
  const rank: Record<CommentSeverity, number> = { critical: 0, major: 1, minor: 2, note: 3 }
  return [...threads].sort((a, b) => {
    const bySeverity = rank[a.severity ?? 'note'] - rank[b.severity ?? 'note']
    if (bySeverity !== 0) return bySeverity
    return (a.anchor?.start ?? -1) - (b.anchor?.start ?? -1)
  })
}

const SEVERITY_STYLE: Record<CommentSeverity, string> = {
  critical: 'text-danger-text border-danger-text/40',
  major: 'text-accent-primary border-accent-primary/40',
  minor: 'text-text-secondary border-border',
  note: 'text-text-tertiary border-border',
}

function CommentThread({
  draftId,
  thread,
  onJump,
}: {
  draftId: number
  thread: DraftComment
  onJump?: (comment: DraftComment) => boolean
}) {
  const reply = useReplyToComment(draftId)
  const resolve = useResolveComment(draftId)
  const address = useStartPass(draftId)
  const [replying, setReplying] = useState(false)
  const [replyText, setReplyText] = useState('')
  const severity = thread.severity ?? 'note'
  const settled = thread.resolved === 1

  async function submitReply() {
    const body = replyText.trim()
    if (!body) return
    try {
      await reply.mutateAsync({ commentId: thread.id, body })
      setReplyText('')
      setReplying(false)
    } catch {
      toast.error('Could not send the reply.')
    }
  }

  return (
    <li
      id={`comment-thread-${thread.id}`}
      tabIndex={-1}
      className="border-border/70 rounded-md border px-3 py-2"
    >
      <div className="mb-1 flex items-center gap-2">
        <span
          className={cn(
            'rounded-sm border px-1.5 py-0 text-[11px] leading-4 font-medium',
            SEVERITY_STYLE[severity],
          )}
        >
          {severity}
        </span>
        {thread.author !== 'reviewer' ? (
          <span className="text-text-tertiary text-xs">{thread.author}</span>
        ) : null}
        <Button
          variant="ghost"
          size="sm"
          className="text-text-tertiary hover:text-text-primary ml-auto h-6 px-2 text-xs"
          disabled={resolve.isPending}
          onClick={async () => {
            try {
              await resolve.mutateAsync({ commentId: thread.id, resolved: !settled })
            } catch {
              toast.error('Could not update the comment.')
            }
          }}
        >
          {settled ? <RotateCcw className="size-3" /> : <Check className="size-3" />}
          {settled ? 'Reopen' : 'Resolve'}
        </Button>
      </div>
      {thread.quote ? (
        // The quote is the anchor, so it is also the way there.
        <blockquote
          className={cn(
            'border-border text-text-tertiary mb-1 border-l-2 pl-2 text-xs italic',
            onJump && 'hover:text-text-secondary cursor-pointer',
          )}
          role={onJump ? 'button' : undefined}
          tabIndex={onJump ? 0 : undefined}
          aria-label={onJump ? 'Show this passage in the document' : undefined}
          onClick={() => {
            if (onJump && !onJump(thread)) {
              toast.info('That passage is not in the document right now.')
            }
          }}
          onKeyDown={(event) => {
            if (onJump && (event.key === 'Enter' || event.key === ' ')) {
              event.preventDefault()
              if (!onJump(thread)) toast.info('That passage is not in the document right now.')
            }
          }}
        >
          {/* Through MathText: a finding about an equation quotes the equation, and a
              blockquote of raw `$\frac{a}{b}$` is not a passage anyone can recognise as
              the one they wrote. */}
          <MathText inline>{clip(thread.quote)}</MathText>
        </blockquote>
      ) : (
        <p className="text-text-tertiary mb-1 text-xs">On the whole document</p>
      )}
      <p className="text-text-primary text-sm leading-5">{thread.body}</p>
      {thread.replies.length > 0 ? (
        <ul className="border-border/70 mt-2 flex flex-col gap-1.5 border-l pl-2.5">
          {thread.replies.map((entry) => (
            <li key={entry.id} className="text-sm leading-5">
              <span className="text-text-tertiary text-xs">
                {entry.author === 'writer' ? 'Lyra' : entry.author}:{' '}
              </span>
              <span className="text-text-secondary">{entry.body}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {settled ? null : replying ? (
        <form
          className="mt-2 flex items-center gap-1.5"
          onSubmit={(event) => {
            event.preventDefault()
            void submitReply()
          }}
        >
          <Input
            value={replyText}
            autoFocus
            placeholder="Reply to this comment"
            aria-label="Reply to this comment"
            className="h-7 text-sm"
            onChange={(event) => setReplyText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                setReplying(false)
                setReplyText('')
              }
            }}
          />
          <Button type="submit" size="sm" className="h-7 px-2.5 text-xs" disabled={reply.isPending}>
            Send
          </Button>
        </form>
      ) : (
        <div className="mt-1 flex flex-wrap items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            className="text-text-tertiary hover:text-text-primary h-6 px-1.5 text-xs"
            onClick={() => setReplying(true)}
          >
            <CornerDownRight className="size-3" />
            Reply
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="text-text-tertiary hover:text-text-primary h-6 px-1.5 text-xs"
            disabled={address.isPending}
            onClick={async () => {
              try {
                await address.mutateAsync({
                  instruction: `Address this review finding: ${thread.body}`,
                  sections: thread.section_ref ? [thread.section_ref] : undefined,
                  address_comment_id: thread.id,
                  depth: 'standard',
                })
                toast.success('Lyra is addressing this comment.')
              } catch {
                toast.error('Could not start the targeted pass.')
              }
            }}
          >
            <Sparkles className="size-3" />
            Address
          </Button>
        </div>
      )}
    </li>
  )
}

/** A long quote is an anchor, not a reprint: enough to recognize the passage. */
function clip(quote: string): string {
  return quote.length <= 160 ? quote : quote.slice(0, 157) + '...'
}
