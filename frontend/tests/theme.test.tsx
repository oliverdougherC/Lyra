import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useLocalStorageState } from '@/lib/hooks/use-local-storage-state'
import { THEME_INIT_SCRIPT, THEME_STORAGE_KEY, ThemeProvider, useTheme } from '@/lib/theme'

/** A `matchMedia` whose result is controllable and whose change listeners actually fire. */
function stubMatchMedia(dark: boolean) {
  const listeners = new Set<() => void>()
  const query = {
    matches: dark,
    media: '(prefers-color-scheme: dark)',
    addEventListener: (_: string, cb: () => void) => listeners.add(cb),
    removeEventListener: (_: string, cb: () => void) => listeners.delete(cb),
  }
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => query),
  )
  return {
    setDark(next: boolean) {
      query.matches = next
      for (const cb of listeners) cb()
    },
  }
}

beforeEach(() => {
  localStorage.clear()
  document.documentElement.classList.remove('dark')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useTheme', () => {
  it('defaults to light for a fresh user', () => {
    stubMatchMedia(false)
    const { result } = renderHook(() => useTheme())
    expect(result.current.theme).toBe('light')
    expect(result.current.resolvedTheme).toBe('light')
  })

  it('reads a stored explicit choice', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'dark')
    stubMatchMedia(false)
    const { result } = renderHook(() => useTheme())
    expect(result.current.resolvedTheme).toBe('dark')
  })

  it('ignores a stored value that is not a theme', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'chartreuse')
    stubMatchMedia(false)
    const { result } = renderHook(() => useTheme())
    expect(result.current.theme).toBe('light')
  })

  it('resolves system against the media query', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'system')
    stubMatchMedia(true)
    const { result } = renderHook(() => useTheme())
    expect(result.current.theme).toBe('system')
    expect(result.current.resolvedTheme).toBe('dark')
  })

  it('follows a live system change while set to system', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'system')
    const media = stubMatchMedia(false)
    const { result } = renderHook(() => useTheme())
    expect(result.current.resolvedTheme).toBe('light')

    act(() => media.setDark(true))
    expect(result.current.resolvedTheme).toBe('dark')
  })

  it('does not follow the system when a theme is chosen explicitly', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'light')
    const media = stubMatchMedia(false)
    const { result } = renderHook(() => useTheme())

    act(() => media.setDark(true))
    expect(result.current.resolvedTheme).toBe('light')
  })

  it('persists a change and moves every subscriber', () => {
    stubMatchMedia(false)
    const first = renderHook(() => useTheme())
    const second = renderHook(() => useTheme())

    act(() => first.result.current.setTheme('dark'))

    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')
    expect(first.result.current.resolvedTheme).toBe('dark')
    // Both hooks read one store, so a change in Settings moves the whole app at once.
    expect(second.result.current.resolvedTheme).toBe('dark')
  })
})

describe('ThemeProvider', () => {
  it('puts the dark class on the document element', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'dark')
    stubMatchMedia(false)
    renderHook(() => null, { wrapper: ThemeProvider })
    expect(document.documentElement).toHaveClass('dark')
  })

  it('removes the dark class when resolving to light', () => {
    document.documentElement.classList.add('dark')
    localStorage.setItem(THEME_STORAGE_KEY, 'light')
    stubMatchMedia(false)
    renderHook(() => null, { wrapper: ThemeProvider })
    expect(document.documentElement).not.toHaveClass('dark')
  })
})

describe('THEME_INIT_SCRIPT', () => {
  it('references the same storage key the hook writes', () => {
    // A drift between the two shows as a flash of the wrong theme on every load.
    expect(THEME_INIT_SCRIPT).toContain(THEME_STORAGE_KEY)
  })

  it('applies dark before first paint for a stored dark choice', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'dark')
    stubMatchMedia(false)
    new Function(THEME_INIT_SCRIPT)()
    expect(document.documentElement).toHaveClass('dark')
  })

  it('applies dark for system when the OS is dark', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'system')
    stubMatchMedia(true)
    new Function(THEME_INIT_SCRIPT)()
    expect(document.documentElement).toHaveClass('dark')
  })

  it('survives localStorage throwing', () => {
    stubMatchMedia(false)
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied')
    })
    // Private-mode storage access throws; a thrown init script blocks the whole document.
    expect(() => new Function(THEME_INIT_SCRIPT)()).not.toThrow()
    getItem.mockRestore()
  })
})

describe('useLocalStorageState', () => {
  const parseFlag = (raw: string) => (raw === 'true' ? true : raw === 'false' ? false : null)

  it('returns the fallback when nothing is stored', () => {
    const { result } = renderHook(() => useLocalStorageState('flag', true, parseFlag))
    expect(result.current[0]).toBe(true)
  })

  it('returns the fallback when the stored value does not parse', () => {
    localStorage.setItem('flag', 'maybe')
    const { result } = renderHook(() => useLocalStorageState('flag', true, parseFlag))
    expect(result.current[0]).toBe(true)
  })

  it('writes through as a string and reads back parsed', () => {
    const { result } = renderHook(() => useLocalStorageState('flag', true, parseFlag))
    act(() => result.current[1](false))
    expect(localStorage.getItem('flag')).toBe('false')
    expect(result.current[0]).toBe(false)
  })

  it('keeps hooks on different keys independent', () => {
    const a = renderHook(() => useLocalStorageState('a', true, parseFlag))
    const b = renderHook(() => useLocalStorageState('b', true, parseFlag))

    act(() => a.result.current[1](false))

    expect(a.result.current[0]).toBe(false)
    expect(b.result.current[0]).toBe(true)
  })
})
