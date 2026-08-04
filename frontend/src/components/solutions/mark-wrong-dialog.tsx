'use client'

import { useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Textarea } from '@/components/ui/textarea'
import type { SolutionPart } from '@/types'

type MarkWrongDialogProps = {
  problem: SolutionPart | null
  onClose: () => void
  onSubmit: (correction: string) => void
  pending: boolean
}

/**
 * One optional textarea. The correction is passed to the model as input and stored as the
 * note on the revision it produces, so it is never silently discarded.
 *
 * Optional because `Regenerate` and this are the same act with and without something to
 * say, and a student who only knows the answer is wrong should not be blocked on
 * articulating why.
 *
 * The caller keys this on the problem, so the draft resets per problem rather than a note
 * written about problem 3 arriving with problem 4.
 */
export function MarkWrongDialog({ problem, onClose, onSubmit, pending }: MarkWrongDialogProps) {
  const [correction, setCorrection] = useState('')

  return (
    <Dialog open={problem !== null} onOpenChange={(open) => (open ? null : onClose())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>What is wrong with it?</DialogTitle>
          <DialogDescription>
            Lyra will use this when it tries {problem?.label ?? 'this problem'} again. You can leave
            it blank.
          </DialogDescription>
        </DialogHeader>
        <Textarea
          value={correction}
          onChange={(event) => setCorrection(event.target.value)}
          rows={4}
          placeholder="Step 3 uses the wrong identity."
          aria-label="What is wrong with this solution"
          autoFocus
        />
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={() => onSubmit(correction.trim())} disabled={pending}>
            {pending ? 'Starting' : 'Solve it again'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
