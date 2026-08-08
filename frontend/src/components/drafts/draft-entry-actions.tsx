'use client'

import { useEffect } from 'react'
import { Sparkles, Wand2 } from 'lucide-react'

import { Button } from '@/components/ui/button'

type DraftEntryActionsProps = {
  onDraftDocument: () => void
  onDraftPassage: () => void
}

/** The two drafting scopes, with the primary action and shortcut sharing one path. */
export function DraftEntryActions({
  onDraftDocument,
  onDraftPassage,
}: DraftEntryActionsProps) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key === '/') {
        event.preventDefault()
        onDraftDocument()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onDraftDocument])

  return (
    <>
      <Button
        variant="ghost"
        size="sm"
        onClick={onDraftDocument}
        title="Draft the complete document (Ctrl-/)"
      >
        <Sparkles className="size-4" />
        Draft document
      </Button>
      <Button
        variant="ghost"
        size="sm"
        onClick={onDraftPassage}
        title="Draft one passage at the cursor"
      >
        <Wand2 className="size-4" />
        Draft passage
      </Button>
    </>
  )
}
