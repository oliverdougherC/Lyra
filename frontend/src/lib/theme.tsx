'use client'

import { useCallback, useEffect, useSyncExternalStore } from 'react'

import { useLocalStorageState } from '@/lib/hooks/use-local-storage-state'

export type Theme = 'system' | 'light' | 'dark'

export const THEME_STORAGE_KEY = 'lyra-theme'

/**
 * Inline script that applies the stored theme before first paint. Without it the document
 * renders light and then swaps, which is visible on every load.
 */
export const THEME_INIT_SCRIPT = `(function(){try{var t=localStorage.getItem('${THEME_STORAGE_KEY}');var d=t==='dark'||(t==='system'&&window.matchMedia('(prefers-color-scheme: dark)').matches);document.documentElement.classList.toggle('dark',d)}catch(e){}})()`

const DARK_QUERY = '(prefers-color-scheme: dark)'

function parseTheme(raw: string): Theme | null {
  return raw === 'system' || raw === 'light' || raw === 'dark' ? raw : null
}

function subscribeToSystemTheme(callback: () => void): () => void {
  const query = window.matchMedia(DARK_QUERY)
  query.addEventListener('change', callback)
  return () => query.removeEventListener('change', callback)
}

export function useTheme() {
  const [theme, setTheme] = useLocalStorageState<Theme>(THEME_STORAGE_KEY, 'light', parseTheme)
  const systemDark = useSyncExternalStore(
    subscribeToSystemTheme,
    useCallback(() => window.matchMedia(DARK_QUERY).matches, []),
    useCallback(() => false, []),
  )

  const resolvedTheme: 'light' | 'dark' =
    theme === 'system' ? (systemDark ? 'dark' : 'light') : theme

  return { theme, resolvedTheme, setTheme }
}

/**
 * Keeps the `.dark` class on `<html>` in step with the resolved theme. Mounted once, near
 * the root; every other component reads `useTheme()` directly, so there is no context.
 */
export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const { resolvedTheme } = useTheme()

  useEffect(() => {
    document.documentElement.classList.toggle('dark', resolvedTheme === 'dark')
  }, [resolvedTheme])

  return children
}
