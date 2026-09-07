'use client'

import { ChevronRight } from 'lucide-react'
import Link from '@/router/link'
import { classReturnHref, usePathname } from '@/router/hooks'
import { SidebarTrigger } from '@/components/ui/sidebar'

import { cn } from '@/lib/utils'
import { useClasses } from '@/lib/hooks/use-classes'

const HEADER_CRUMB_SLOT = 'lyra-header-crumb'
const HEADER_ACTIONS_SLOT = 'lyra-header-actions'

type Crumb = {
  label: string
  href?: string
  detail?: string
}

function readClassId(pathname: string): number | null {
  const match = /^\/classes\/(\d+)/.exec(pathname)
  if (!match) return null
  const classId = Number(match[1])
  return Number.isSafeInteger(classId) && classId > 0 ? classId : null
}

/**
 * The application's lintel: the one bar the shell never takes away, carrying the
 * breadcrumb, the route's portaled title and actions.
 *
 * What it used to hold on a class route - the endpoint readout, the Profile button and its
 * sheet - is gone. The endpoint readout moved to Settings, where the endpoint is actually
 * changed; the profile moved into the class hub's own menu, where the class is the subject.
 * The bar keeps the skip link, the crumb, and the route slots, and it is `inert` in
 * immersive mode so a keyboard user is not tabbed into a control the screen does not show.
 */
export function AppHeader({ collapsed = false }: { collapsed?: boolean }) {
  const pathname = usePathname()
  const { data: classes } = useClasses()

  const classId = readClassId(pathname)
  const klass = classId !== null ? (classes?.find((item) => item.id === classId) ?? null) : null
  // The class now has a page of its own, so on any route inside it the class crumb is the
  // way back up to that page rather than a label naming where you already are.
  const insideClass = classId !== null && pathname !== `/classes/${classId}`
  const classHref = insideClass ? classReturnHref(classId) : undefined

  // Settings is a root, not a child of Classes: nothing about it lives inside a class.
  const crumbs: Crumb[] =
    pathname === '/settings'
      ? [{ label: 'Settings' }]
      : [
          { label: 'Classes', href: '/' },
          ...(klass
            ? [{ label: klass.name, detail: klass.code ?? undefined, href: classHref }]
            : classId !== null
              ? [{ label: 'Class', href: classHref }]
              : []),
        ]

  return (
    <header
      data-app-header
      data-collapsed={collapsed || undefined}
      // `inert` rather than only hidden: a bar with no height still holds focusable
      // controls, and tabbing into a breadcrumb nobody can see is how a keyboard user
      // ends up somewhere the screen does not show them.
      inert={collapsed}
      className={cn(
        'sticky top-0 z-10 flex shrink-0 items-center gap-2 bg-background/85 px-4 transition-[height,transform,opacity] duration-200 ease-out supports-[backdrop-filter]:backdrop-blur-sm',
        collapsed
          ? 'relative h-0 -translate-y-full overflow-hidden border-b-0 opacity-0'
          : 'relative h-14 translate-y-0 border-b opacity-100',
      )}
    >
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
                  title={crumb.detail ? `${crumb.detail} · ${crumb.label}` : crumb.label}
                  className="flex min-w-0 items-baseline gap-1.5 rounded-sm text-text-secondary transition-colors duration-150 hover:text-text-primary focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none"
                >
                  {/* The course code travels with the name whether or not the crumb links
                      anywhere: it is half of what identifies the class, and dropping it on
                      the routes inside the class was the one place it disappeared. */}
                  {crumb.detail ? <span className="shrink-0">{crumb.detail}</span> : null}
                  <span className="truncate">{crumb.label}</span>
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
          {/* Where a workspace route puts its own title. It is a crumb rather than a
              heading of its own, because a heading would cost the route a second bar to
              put it in and this one is already here. */}
          <li id={HEADER_CRUMB_SLOT} className="contents" />
        </ol>
      </nav>

      {/* The route's own actions, ahead of the class-level ones: they belong to what is on
          screen, and a workspace that gives up its title row has nowhere else to put them. */}
      <div id={HEADER_ACTIONS_SLOT} className="flex shrink-0 items-center gap-1" />
    </header>
  )
}
