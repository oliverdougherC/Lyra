'use client'

import { useCallback, useSyncExternalStore } from 'react'

type Primitive = string | number | boolean

const listeners = new Map<string, Set<() => void>>()

function notify(key: string): void {
  for (const listener of listeners.get(key) ?? []) listener()
}

/**
 * Client state backed by `localStorage`, read through `useSyncExternalStore` so the
 * server snapshot is the fallback and the stored value is picked up on hydration without
 * a setState-in-effect cascade.
 *
 * Only primitives are supported: `getSnapshot` must return a referentially stable value,
 * and an object parsed fresh on every render would not be.
 */
export function useLocalStorageState<T extends Primitive>(
  key: string,
  fallback: T,
  parse: (raw: string) => T | null,
): [T, (next: T) => void] {
  const subscribe = useCallback(
    (callback: () => void) => {
      let forKey = listeners.get(key)
      if (!forKey) {
        forKey = new Set()
        listeners.set(key, forKey)
      }
      forKey.add(callback)
      // Another tab writing the same key should move this one too.
      window.addEventListener('storage', callback)
      return () => {
        forKey.delete(callback)
        window.removeEventListener('storage', callback)
      }
    },
    [key],
  )

  const getSnapshot = useCallback(() => {
    const raw = localStorage.getItem(key)
    if (raw === null) return fallback
    return parse(raw) ?? fallback
  }, [key, fallback, parse])

  const getServerSnapshot = useCallback(() => fallback, [fallback])

  const value = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot)

  const setValue = useCallback(
    (next: T) => {
      localStorage.setItem(key, String(next))
      notify(key)
    },
    [key],
  )

  return [value, setValue]
}
