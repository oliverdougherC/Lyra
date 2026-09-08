'use client'

import { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { createScrollPositions } from '@/router/scroll-positions'

type RouteParams = Record<string, string>

type RouterOptions = {
  scroll?: boolean
}

type RouterContextValue = {
  pathname: string
  search: string
  searchParams: URLSearchParams
  params: RouteParams
  navigationVersion: number
  navigate: (href: string, mode: 'push' | 'replace', options?: RouterOptions) => void
  setAnchor: (anchor: string | null, mode: 'push' | 'replace') => void
}

export type RouterHandle = {
  push: (href: string, options?: RouterOptions) => void
  replace: (href: string, options?: RouterOptions) => void
  pushAnchor: (anchor: string) => void
  replaceAnchor: (anchor: string | null) => void
  back: () => void
  forward: () => void
  reload: () => void
  prefetch: (_href: string) => Promise<void>
}

export const RouterContext = createContext<RouterContextValue | null>(null)
export const ROUTE_ANCHOR_QUERY_KEY = 'lyra-anchor'

type RouteState = {
  pathname: string
  search: string
  params: RouteParams
  navigationVersion: number
  scrollPositions?: Record<string, number>
}

function normalizePath(path: string): string {
  if (!path) return '/'
  if (path.startsWith('/')) return path
  return `/${path}`
}

function splitHref(href: string): { pathname: string; search: string; anchor: string | null } {
  const [pathPart, hashFragment = ''] = href.split('#', 2)
  const [pathname, search = ''] = pathPart.split('?', 2)
  const anchor = normalizeAnchor(hashFragment)
  return {
    pathname: normalizePath(pathname),
    search: search ? `?${search}` : '',
    anchor,
  }
}

function createHashHref(pathname: string, search: string): string {
  return `/#${pathname}${search}`
}

type MatchedRoute = {
  params: RouteParams
}

function matchRoute(pathname: string): MatchedRoute {
  const segments = pathname.split('/').filter(Boolean)
  const params: RouteParams = {}

  if (segments[0] !== 'classes') return { params }
  if (segments[1]) params.id = segments[1]
  if (segments[2] === 'solutions' && segments[3]) {
    params.artifactId = segments[3]
  }
  if (segments[2] === 'study' && segments[3]) {
    params.artifactId = segments[3]
  }
  if (segments[2] === 'drafts' && segments[3]) {
    params.artifactId = segments[3]
  }

  return { params }
}

function normalizeAnchor(anchor: string): string | null {
  if (!anchor) return null
  const trimmed = anchor.trim()
  if (!trimmed) return null
  return /^[A-Za-z0-9][A-Za-z0-9:_-]*$/.test(trimmed) ? trimmed : null
}

function withRouteAnchor(search: string, anchor: string | null): string {
  const params = new URLSearchParams(search.startsWith('?') ? search.slice(1) : '')
  if (anchor) params.set(ROUTE_ANCHOR_QUERY_KEY, anchor)
  else params.delete(ROUTE_ANCHOR_QUERY_KEY)
  const next = params.toString()
  return next ? `?${next}` : ''
}

function routeAnchorFromSearch(search: string): string | null {
  const params = new URLSearchParams(search.startsWith('?') ? search.slice(1) : '')
  return normalizeAnchor(params.get(ROUTE_ANCHOR_QUERY_KEY) ?? '')
}

function readLocation(
  fallback?: Pick<RouteState, 'pathname' | 'search'>,
): Omit<RouteState, 'navigationVersion'> {
  if (typeof window === 'undefined') return { pathname: '/', search: '', params: {} }

  const hash = window.location.hash
  if (hash.startsWith('#/')) {
    const { pathname, search, anchor } = splitHref(hash.slice(1))
    const nextSearch = anchor ? withRouteAnchor(search, anchor) : search
    return { pathname, search: nextSearch, params: matchRoute(pathname).params }
  }

  const hashAnchor = normalizeAnchor(hash.startsWith('#') ? hash.slice(1) : '')
  if (hashAnchor && fallback) {
    const pathname = fallback.pathname
    const search = withRouteAnchor(withRouteAnchor(fallback.search, null), hashAnchor)
    return { pathname, search, params: matchRoute(pathname).params }
  }

  const pathname = normalizePath(window.location.pathname)
  const search = hashAnchor
    ? withRouteAnchor(window.location.search, hashAnchor)
    : window.location.search
  return { pathname, search, params: matchRoute(pathname).params }
}

function changeLocation(method: 'pushState' | 'replaceState', data: unknown, href: string) {
  try {
    window.history[method](data, '', href)
  } catch {
    // If the browser refuses even a real navigation, use same-document hash navigation.
    // This does not reload the app or leave its URL out of sync with the selected route.
    const hash = href.slice(href.indexOf('#'))
    if (method === 'replaceState') window.location.replace(hash)
    else window.location.hash = hash
  }
}

const ENTRY_KEY = 'lyraEntry'
function entryId(): string | undefined {
  const value = window.history.state?.[ENTRY_KEY]
  return typeof value === 'string' ? value : undefined
}

const SCROLL_KEY = 'lyraScroll'
const SCROLL_TARGETS: Record<string, string> = {
  main: '#main-content',
  files: '#documents-pane-body [data-slot="scroll-area-viewport"]',
}

function readScrollPositions(): Record<string, number> {
  const positions: Record<string, number> = {}
  for (const [key, selector] of Object.entries(SCROLL_TARGETS)) {
    const element = document.querySelector<HTMLElement>(selector)
    if (element) positions[key] = element.scrollTop
  }
  return positions
}

function rememberClassReturn(pathname: string, search: string) {
  const match = /^\/classes\/(\d+)$/.exec(pathname)
  if (!match) return
  try {
    sessionStorage.setItem(
      `lyra:class:${match[1]}:return`,
      JSON.stringify({
        href: pathname + search,
        positions: readScrollPositions(),
      }),
    )
  } catch {
    /* History navigation still works without session storage. */
  }
}

export function classReturnHref(classId: number): string {
  try {
    const saved = JSON.parse(sessionStorage.getItem(`lyra:class:${classId}:return`) ?? 'null')
    if (
      typeof saved?.href === 'string' &&
      new RegExp(`^/classes/${classId}(\\?|$)`).test(saved.href)
    )
      return saved.href
  } catch {
    /* Use the saved-work destination if context cannot be read. */
  }
  return `/classes/${classId}?tab=work`
}

function classReturnPositions(
  pathname: string,
  search: string,
): Record<string, number> | undefined {
  const match = /^\/classes\/(\d+)$/.exec(pathname)
  if (!match) return
  try {
    const saved = JSON.parse(sessionStorage.getItem(`lyra:class:${match[1]}:return`) ?? 'null')
    if (saved?.href === pathname + search) return saved.positions
  } catch {
    /* A new visit starts at the top. */
  }
}

export function RouterProvider({ children }: { children: React.ReactNode }) {
  const [positions] = useState(createScrollPositions)
  const currentEntry = useRef(entryId() ?? crypto.randomUUID())
  const [state, setState] = useState<RouteState>(() => ({
    ...readLocation(),
    navigationVersion: 0,
    scrollPositions: positions.read(currentEntry.current) ?? window.history.state?.[SCROLL_KEY],
  }))
  const currentHref = useRef(createHashHref(state.pathname, state.search))
  const stateRef = useRef(state)
  stateRef.current = state

  useEffect(() => {
    const previous = window.history.scrollRestoration
    window.history.scrollRestoration = 'manual'
    const capture = (event: Event) => {
      // Ignore unrelated nested scrollers, including programmatic ones.
      if (
        !(event.target instanceof Element) ||
        !Object.values(SCROLL_TARGETS).some(
          (selector) => event.target === document.querySelector(selector),
        )
      )
        return
      positions.record(currentEntry.current, readScrollPositions())
    }
    const checkpoint = () => {
      positions.record(currentEntry.current, readScrollPositions())
      positions.flush()
    }
    document.addEventListener('scroll', capture, true)
    window.addEventListener('pagehide', checkpoint)
    return () => {
      document.removeEventListener('scroll', capture, true)
      window.removeEventListener('pagehide', checkpoint)
      positions.flush()
      window.history.scrollRestoration = previous
    }
  }, [positions])

  useEffect(() => {
    const positions = state.scrollPositions
    if (!positions || routeAnchorFromSearch(state.search)) return
    // Lazy routes can mount after navigation. Stop when positions fit or on user input.
    let stopped = false
    const restore = () => {
      if (stopped) return
      let complete = true
      for (const [key, top] of Object.entries(positions)) {
        const selector = SCROLL_TARGETS[key]
        const element = selector ? document.querySelector<HTMLElement>(selector) : null
        if (!element) {
          complete = false
          continue
        }
        element.scrollTop = top
        if (Math.abs(element.scrollTop - top) > 1) complete = false
      }
      if (complete) stop()
    }
    const observer = new MutationObserver(restore)
    const stop = () => {
      stopped = true
      observer.disconnect()
    }
    observer.observe(document.body, { childList: true, subtree: true })
    const frame = requestAnimationFrame(restore)
    window.addEventListener('wheel', stop, { once: true })
    window.addEventListener('touchstart', stop, { once: true })
    window.addEventListener('keydown', stop, { once: true })
    return () => {
      stop()
      cancelAnimationFrame(frame)
      window.removeEventListener('wheel', stop)
      window.removeEventListener('touchstart', stop)
      window.removeEventListener('keydown', stop)
    }
  }, [state.navigationVersion, state.scrollPositions, state.search])

  useEffect(() => {
    const sync = () => {
      positions.flush()
      const next = readLocation(stateRef.current)
      const href = createHashHref(next.pathname, next.search)
      const id =
        entryId() ?? (href === currentHref.current ? currentEntry.current : crypto.randomUUID())
      currentEntry.current = id
      currentHref.current = href
      // One identity stamp on a fresh/legacy entry, never on scroll. Refusal is optional.
      if (
        entryId() !== id ||
        `${window.location.pathname}${window.location.search}${window.location.hash}` !== href
      ) {
        try {
          window.history.replaceState({ ...window.history.state, [ENTRY_KEY]: id }, '', href)
        } catch {
          /* Keep the hash route usable. */
        }
      }
      setState((current) => ({
        ...next,
        navigationVersion: current.navigationVersion + 1,
        scrollPositions: positions.read(id) ?? window.history.state?.[SCROLL_KEY],
      }))
    }
    sync()
    window.addEventListener('hashchange', sync)
    window.addEventListener('popstate', sync)
    return () => {
      window.removeEventListener('hashchange', sync)
      window.removeEventListener('popstate', sync)
    }
  }, [positions])

  const value = useMemo<RouterContextValue>(
    () => ({
      pathname: state.pathname,
      search: state.search,
      searchParams: new URLSearchParams(state.search.startsWith('?') ? state.search.slice(1) : ''),
      params: state.params,
      navigationVersion: state.navigationVersion,
      navigate: (href, mode, options) => {
        const { pathname, search, anchor } = splitHref(href)
        // An href can carry the anchor in either form: a fragment (the editor's source-jump
        // idiom) or the reserved query parameter (the form attention destinations are built
        // in). Both arrive as `lyra-anchor`; a navigation that names no anchor at all
        // clears the previous one rather than letting it leak into the next route.
        const carried = anchor ?? routeAnchorFromSearch(search)
        const nextSearch = carried
          ? withRouteAnchor(search, carried)
          : withRouteAnchor(search, null)
        const method = mode === 'replace' ? 'replaceState' : 'pushState'
        positions.record(currentEntry.current, readScrollPositions())
        positions.flush()
        rememberClassReturn(state.pathname, state.search)
        const scrollPositions =
          options?.scroll === false || carried !== null
            ? readScrollPositions()
            : (classReturnPositions(pathname, nextSearch) ?? { main: 0 })
        const id = mode === 'push' ? crypto.randomUUID() : currentEntry.current
        const nextHref = createHashHref(pathname, nextSearch)
        changeLocation(
          method,
          { ...window.history.state, [ENTRY_KEY]: id, [SCROLL_KEY]: scrollPositions },
          nextHref,
        )
        currentEntry.current = id
        currentHref.current = nextHref
        positions.record(id, scrollPositions)
        setState((current) => ({
          pathname,
          search: nextSearch,
          params: matchRoute(pathname).params,
          scrollPositions,
          navigationVersion: current.navigationVersion + 1,
        }))
      },
      setAnchor: (anchor, mode) => {
        const nextSearch = withRouteAnchor(
          withRouteAnchor(state.search, null),
          normalizeAnchor(anchor ?? ''),
        )
        const next = createHashHref(state.pathname, nextSearch)
        const method = mode === 'replace' ? 'replaceState' : 'pushState'
        const scrollPositions = readScrollPositions()
        positions.record(currentEntry.current, scrollPositions)
        positions.flush()
        const id = mode === 'push' ? crypto.randomUUID() : currentEntry.current
        changeLocation(
          method,
          { ...window.history.state, [ENTRY_KEY]: id, [SCROLL_KEY]: scrollPositions },
          next,
        )
        currentEntry.current = id
        currentHref.current = next
        positions.record(id, scrollPositions)
        setState((current) => ({
          pathname: current.pathname,
          search: nextSearch,
          params: current.params,
          navigationVersion: current.navigationVersion + 1,
        }))
      },
    }),
    [state, positions],
  )

  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>
}

function useRouterContext(): RouterContextValue {
  const value = useContext(RouterContext)
  if (!value) throw new Error('Router hooks must be used inside RouterProvider.')
  return value
}

export function usePathname(): string {
  return useRouterContext().pathname
}

export function useSearchParams(): URLSearchParams {
  return useRouterContext().searchParams
}

export function useRouteAnchor(): string | null {
  return routeAnchorFromSearch(useRouterContext().search)
}

export function useNavigationVersion(): number {
  return useRouterContext().navigationVersion
}

export function useParams<T extends RouteParams = RouteParams>(): T {
  return useRouterContext().params as T
}

export function useRouter(): RouterHandle {
  const context = useRouterContext()
  return useMemo(
    () => ({
      push: (href: string, options?: RouterOptions) => context.navigate(href, 'push', options),
      replace: (href: string, options?: RouterOptions) =>
        context.navigate(href, 'replace', options),
      pushAnchor: (anchor: string) => context.setAnchor(anchor, 'push'),
      replaceAnchor: (anchor: string | null) => context.setAnchor(anchor, 'replace'),
      back: () => window.history.back(),
      forward: () => window.history.forward(),
      reload: () => window.location.reload(),
      prefetch: async () => {},
    }),
    [context],
  )
}
