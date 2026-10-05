'use client'

import { useCallback, useSyncExternalStore } from 'react'

type Primitive = string | number | boolean

const listeners = new Map<string, Set<() => void>>()
// Preferences still work for this window when storage is denied or full.
const unsavedValues = new Map<string, string>()

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
      const onStorage = (event: StorageEvent) => {
        if (event.key !== null && event.key !== key) return
        unsavedValues.delete(key)
        callback()
      }
      window.addEventListener('storage', onStorage)
      return () => {
        forKey.delete(callback)
        if (forKey.size === 0) listeners.delete(key)
        window.removeEventListener('storage', onStorage)
      }
    },
    [key],
  )

  const getSnapshot = useCallback(() => {
    try {
      const raw = unsavedValues.get(key) ?? localStorage.getItem(key)
      if (raw === null) return fallback
      return parse(raw) ?? fallback
    } catch {
      return fallback
    }
  }, [key, fallback, parse])

  const getServerSnapshot = useCallback(() => fallback, [fallback])

  const value = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot)

  const setValue = useCallback(
    (next: T) => {
      const raw = String(next)
      try {
        localStorage.setItem(key, raw)
        unsavedValues.delete(key)
      } catch {
        unsavedValues.set(key, raw)
      }
      notify(key)
    },
    [key],
  )

  return [value, setValue]
}
