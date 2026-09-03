'use client'

import { useEffect } from 'react'

/**
 * The whole-document drafting shortcut (Ctrl/Cmd + /).
 *
 * The drafting actions themselves live in the draft header's More menu, but the shortcut
 * keeps the primary path one keystroke away from anywhere on the page - a menu item has to
 * be found, a shortcut does not. The binding is the component's only concern now, so it
 * ships as a hook the draft page registers once.
 */
export function useDraftDocumentShortcut(onDraftDocument: () => void) {
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
}
