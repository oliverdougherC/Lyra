'use client'

import { createContext, useContext, useEffect, useMemo, useState } from 'react'

type RouteParams = Record<string, string>

type RouterOptions = {
  scroll?: boolean
}

type RouterContextValue = {
  pathname: string
  search: string
  searchParams: URLSearchParams
  params: RouteParams
  navigate: (href: string, mode: 'push' | 'replace', options?: RouterOptions) => void
}

export type RouterHandle = {
  push: (href: string, options?: RouterOptions) => void
  replace: (href: string, options?: RouterOptions) => void
  back: () => void
  forward: () => void
  reload: () => void
  prefetch: (_href: string) => Promise<void>
}

export const RouterContext = createContext<RouterContextValue | null>(null)

function normalizePath(path: string): string {
  if (!path) return '/'
  if (path.startsWith('/')) return path
  return `/${path}`
}

function splitHref(href: string): { pathname: string; search: string } {
  const [pathPart, hashFragment = ''] = href.split('#', 2)
  const [pathname, search = ''] = pathPart.split('?', 2)
  const suffix = hashFragment ? `#${hashFragment}` : ''
  return {
    pathname: normalizePath(pathname),
    search: search ? `?${search}${suffix}` : suffix,
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
  if (segments[1] && /^\d+$/.test(segments[1])) params.id = segments[1]
  if (segments[2] === 'solutions' && segments[3] && /^\d+$/.test(segments[3])) {
    params.artifactId = segments[3]
  }
  if (segments[2] === 'study' && segments[3] && /^\d+$/.test(segments[3])) {
    params.artifactId = segments[3]
  }
  if (segments[2] === 'drafts' && segments[3] && /^\d+$/.test(segments[3])) {
    params.artifactId = segments[3]
  }

  return { params }
}

function readLocation(): { pathname: string; search: string; params: RouteParams } {
  if (typeof window === 'undefined') return { pathname: '/', search: '', params: {} }

  const hash = window.location.hash
  if (hash.startsWith('#/')) {
    const { pathname, search } = splitHref(hash.slice(1))
    return { pathname, search, params: matchRoute(pathname).params }
  }

  const pathname = normalizePath(window.location.pathname)
  const search = window.location.search
  return { pathname, search, params: matchRoute(pathname).params }
}

function maybeNormalizeInitialHash() {
  if (typeof window === 'undefined') return
  if (window.location.hash.startsWith('#/')) return
  const pathname = normalizePath(window.location.pathname)
  const search = window.location.search
  window.history.replaceState(window.history.state, '', createHashHref(pathname, search))
}

function restoreScroll(scroll = true) {
  if (scroll) window.scrollTo({ top: 0, left: 0, behavior: 'auto' })
}

export function RouterProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState(() => readLocation())

  useEffect(() => {
    maybeNormalizeInitialHash()
    const sync = () => setState(readLocation())
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
      navigate: (href, mode, options) => {
        const { pathname, search } = splitHref(href)
        const next = createHashHref(pathname, search)
        const method = mode === 'replace' ? 'replaceState' : 'pushState'
        window.history[method](window.history.state, '', next)
        setState({ pathname, search, params: matchRoute(pathname).params })
        restoreScroll(options?.scroll)
        window.dispatchEvent(new HashChangeEvent('hashchange'))
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
      back: () => window.history.back(),
      forward: () => window.history.forward(),
      reload: () => window.location.reload(),
      prefetch: async () => {},
    }),
    [context],
  )
}
