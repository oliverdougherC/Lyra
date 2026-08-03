'use client'

import { useCallback, useSyncExternalStore } from 'react'

/**
 * Reads a media query through `useSyncExternalStore`, so there is no setState cascade on
 * mount and the server snapshot is a definite `false` rather than `undefined`.
 */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (callback: () => void) => {
      const list = window.matchMedia(query)
      list.addEventListener('change', callback)
      return () => list.removeEventListener('change', callback)
    },
    [query],
  )

  return useSyncExternalStore(
    subscribe,
    useCallback(() => window.matchMedia(query).matches, [query]),
    useCallback(() => false, []),
  )
}
