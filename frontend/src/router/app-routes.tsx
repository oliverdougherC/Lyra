'use client'

import React from 'react'
import { lazy, Suspense, useMemo } from 'react'

import RouteErrorFallback from '@/app/error'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Skeleton } from '@/components/ui/skeleton'
import { usePathname } from '@/router/hooks'

type RoutePattern =
  | '/'
  | '/settings'
  | '/classes/:id'
  | '/classes/:id/chat'
  | '/classes/:id/solutions'
  | '/classes/:id/solutions/new'
  | '/classes/:id/solutions/:artifactId'
  | '/classes/:id/study/:artifactId'
  | '/classes/:id/drafts/:artifactId'

type RouteDefinition = {
  id: string
  pattern: RoutePattern
  component: React.LazyExoticComponent<React.ComponentType>
}

const routes: RouteDefinition[] = [
  { id: 'home', pattern: '/', component: lazy(() => import('@/app/page')) },
  { id: 'settings', pattern: '/settings', component: lazy(() => import('@/app/settings/page')) },
  {
    id: 'class-hub',
    pattern: '/classes/:id',
    component: lazy(() => import('@/app/classes/[id]/page')),
  },
  {
    id: 'class-chat',
    pattern: '/classes/:id/chat',
    component: lazy(() => import('@/app/classes/[id]/chat/page')),
  },
  {
    id: 'solutions-index',
    pattern: '/classes/:id/solutions',
    component: lazy(() => import('@/app/classes/[id]/solutions/page')),
  },
  {
    id: 'solutions-new',
    pattern: '/classes/:id/solutions/new',
    component: lazy(() => import('@/app/classes/[id]/solutions/new/page')),
  },
  {
    id: 'solutions-workspace',
    pattern: '/classes/:id/solutions/:artifactId',
    component: lazy(() => import('@/app/classes/[id]/solutions/[artifactId]/page')),
  },
  {
    id: 'study-session',
    pattern: '/classes/:id/study/:artifactId',
    component: lazy(() => import('@/app/classes/[id]/study/[artifactId]/page')),
  },
  {
    id: 'draft-workspace',
    pattern: '/classes/:id/drafts/:artifactId',
    component: lazy(() => import('@/app/classes/[id]/drafts/[artifactId]/page')),
  },
]

function matchPattern(pattern: RoutePattern, pathname: string): boolean {
  const patternParts = pattern.split('/').filter(Boolean)
  const pathParts = pathname.split('/').filter(Boolean)
  if (patternParts.length !== pathParts.length) return false
  return patternParts.every((part, index) => part.startsWith(':') || part === pathParts[index])
}

class RouteBoundary extends React.Component<
  { routeKey: string; children: React.ReactNode },
  { error: Error | null }
> {
  state = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidUpdate(prevProps: { routeKey: string }) {
    if (prevProps.routeKey !== this.props.routeKey && this.state.error) {
      this.setState({ error: null })
    }
  }

  render() {
    if (this.state.error) {
      return <RouteErrorFallback error={this.state.error} retry={() => window.location.reload()} />
    }
    return this.props.children
  }
}

function NotFound() {
  return (
    <Alert variant="destructive" className="max-w-xl">
      <AlertTitle>That page could not be opened</AlertTitle>
      <AlertDescription>Use the sidebar or return to Classes to keep working.</AlertDescription>
    </Alert>
  )
}

function RouteFallback() {
  return (
    <div className="mx-auto flex w-full max-w-[860px] flex-col gap-4 pt-6" aria-busy="true">
      <Skeleton className="h-8 w-48" />
      <Skeleton className="h-4 w-80" />
      <Skeleton className="h-64 w-full rounded-md" />
    </div>
  )
}

export function AppRoutes() {
  const pathname = usePathname()
  const activeRoute = useMemo(
    () => routes.find((route) => matchPattern(route.pattern, pathname)) ?? null,
    [pathname],
  )
  if (!activeRoute) return <NotFound />

  const Component = activeRoute.component

  return (
    <RouteBoundary routeKey={pathname}>
      <Suspense fallback={<RouteFallback />}>
        <Component />
      </Suspense>
    </RouteBoundary>
  )
}
