'use client'

import { useCallback, useRef, useState } from 'react'
import { ChevronRight, UserRound } from 'lucide-react'
import Link from 'next/link'
import { usePathname } from 'next/navigation'

import { EndpointLocalityBadge } from '@/components/layout/endpoint-locality-badge'
import { ClassProfileSheet } from '@/components/profile/class-profile-sheet'
import { Button } from '@/components/ui/button'
import { SidebarTrigger } from '@/components/ui/sidebar'
import { cn } from '@/lib/utils'
import { useClasses } from '@/lib/hooks/use-classes'

type Crumb = { label: string; href?: string; detail?: string }

function readClassId(pathname: string): number | null {
  const match = /^\/classes\/(\d+)/.exec(pathname)
  if (!match) return null
  const classId = Number(match[1])
  return Number.isSafeInteger(classId) && classId > 0 ? classId : null
}

/**
 * The header carries the breadcrumb, and on class pages the class code and its profile
 * button, so the workspace below starts right at the panes.
 */
export function AppHeader() {
  const pathname = usePathname()
  const { data: classes } = useClasses()
  const [profileOpen, setProfileOpen] = useState(false)
  const profileTriggerRef = useRef<HTMLButtonElement>(null)
  const onProfileOpenChange = useCallback((open: boolean) => {
    setProfileOpen(open)
    if (!open) requestAnimationFrame(() => profileTriggerRef.current?.focus())
  }, [])

  const classId = readClassId(pathname)
  const klass = classId !== null ? (classes?.find((item) => item.id === classId) ?? null) : null

  // Settings is a root, not a child of Classes: nothing about it lives inside a class.
  const crumbs: Crumb[] =
    pathname === '/settings'
      ? [{ label: 'Settings' }]
      : [
          { label: 'Classes', href: '/' },
          ...(klass
            ? [{ label: klass.name, detail: klass.code ?? undefined }]
            : classId !== null
              ? [{ label: 'Class' }]
              : []),
        ]

  return (
    <header className="sticky top-0 z-10 flex h-14 shrink-0 items-center gap-2 border-b bg-background/85 px-4 supports-[backdrop-filter]:backdrop-blur-sm">
      <SidebarTrigger className="-ml-1" />
      <nav aria-label="Breadcrumb" className="min-w-0 flex-1">
        <ol className="flex items-center gap-1.5 text-[13px]">
          {crumbs.map((crumb, index) => (
            <li
              key={crumb.label}
              // Ancestors fold away on small screens: three crumbs in 375px truncates
              // every one of them, and the current page is the one worth reading.
              className={cn(
                'min-w-0 items-center gap-1.5',
                index < crumbs.length - 1 ? 'hidden sm:flex' : 'flex',
              )}
            >
              {index > 0 && (
                <ChevronRight
                  aria-hidden
                  className="hidden size-3.5 shrink-0 text-text-tertiary sm:block"
                />
              )}
              {crumb.href ? (
                <Link
                  href={crumb.href}
                  className="truncate rounded-sm text-text-secondary transition-colors duration-150 hover:text-text-primary focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none"
                >
                  {crumb.label}
                </Link>
              ) : (
                <span
                  aria-current="page"
                  className="flex min-w-0 items-baseline gap-1.5"
                  title={crumb.detail ? `${crumb.detail} · ${crumb.label}` : crumb.label}
                >
                  {crumb.detail ? (
                    <span className="text-text-secondary shrink-0">{crumb.detail}</span>
                  ) : null}
                  <span className="truncate font-medium">{crumb.label}</span>
                </span>
              )}
            </li>
          ))}
        </ol>
      </nav>

      {klass ? (
        <div className="flex shrink-0 items-center gap-2">
          <Button
            ref={profileTriggerRef}
            variant="outline"
            size="sm"
            onClick={() => setProfileOpen(true)}
          >
            <UserRound aria-hidden className="size-3.5" />
            Profile
          </Button>
          <ClassProfileSheet
            classId={klass.id}
            open={profileOpen}
            onOpenChange={onProfileOpenChange}
          />
        </div>
      ) : null}
      <EndpointLocalityBadge />
    </header>
  )
}
