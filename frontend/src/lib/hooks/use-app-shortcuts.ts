'use client'

import { useEffect } from 'react'

/**
 * The application-wide shortcuts from the keyboard map in docs/ui-phase-1.md.
 *
 * Cmd/Ctrl+B lives in the sidebar primitive and is not repeated here. No binding shadows
 * a browser default, and none fire while the user is typing except the ones whose whole
 * purpose is to move focus out of a field.
 */
type Shortcut = {
  key: string
  /** Runs even when focus is inside a text field. */
  allowInEditable?: boolean
  run: () => void
}

function isEditable(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  const tag = target.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
}

export function useAppShortcuts(shortcuts: Shortcut[]): void {
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (!(event.metaKey || event.ctrlKey) || event.altKey) return
      const match = shortcuts.find(
        (shortcut) => shortcut.key.toLowerCase() === event.key.toLowerCase(),
      )
      if (!match) return
      if (!match.allowInEditable && isEditable(event.target)) return
      event.preventDefault()
      match.run()
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [shortcuts])
}
