'use client'

import { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react'

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

function canonicalizeLocation(next: Pick<RouteState, 'pathname' | 'search'>) {
  if (typeof window === 'undefined') return
  const canonical = createHashHref(next.pathname, next.search)
  if (`${window.location.pathname}${window.location.search}${window.location.hash}` === canonical) {
    return
  }
  window.history.replaceState(window.history.state, '', canonical)
}

function restoreScroll(scroll = true) {
  if (scroll) window.scrollTo({ top: 0, left: 0, behavior: 'auto' })
}

export function RouterProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<RouteState>(() => ({
    ...readLocation(),
    navigationVersion: 0,
  }))
  const stateRef = useRef(state)
  stateRef.current = state

  useEffect(() => {
    const sync = () => {
      const next = readLocation(stateRef.current)
      canonicalizeLocation(next)
      setState((current) => ({ ...next, navigationVersion: current.navigationVersion + 1 }))
    }
    sync()
    window.addEventListener('hashchange', sync)
    window.addEventListener('popstate', sync)
    return () => {
      window.removeEventListener('hashchange', sync)
      window.removeEventListener('popstate', sync)
    }
  }, [])

  const value = useMemo<RouterContextValue>(
    () => ({
      pathname: state.pathname,
      search: state.search,
      searchParams: new URLSearchParams(state.search.startsWith('?') ? state.search.slice(1) : ''),
      params: state.params,
      navigationVersion: state.navigationVersion,
      navigate: (href, mode, options) => {
        const { pathname, search, anchor } = splitHref(href)
        const nextSearch = anchor ? withRouteAnchor(search, anchor) : withRouteAnchor(search, null)
        const method = mode === 'replace' ? 'replaceState' : 'pushState'
        window.history[method](window.history.state, '', createHashHref(pathname, nextSearch))
        setState((current) => ({
          pathname,
          search: nextSearch,
          params: matchRoute(pathname).params,
          navigationVersion: current.navigationVersion + 1,
        }))
        restoreScroll(options?.scroll ?? anchor === null)
      },
      setAnchor: (anchor, mode) => {
        const nextSearch = withRouteAnchor(
          withRouteAnchor(state.search, null),
          normalizeAnchor(anchor ?? ''),
        )
        const next = createHashHref(state.pathname, nextSearch)
        const method = mode === 'replace' ? 'replaceState' : 'pushState'
        window.history[method](window.history.state, '', next)
        setState((current) => ({
          pathname: current.pathname,
          search: nextSearch,
          params: current.params,
          navigationVersion: current.navigationVersion + 1,
        }))
      },
    }),
    [state],
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
