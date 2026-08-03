'use client'

import { ChevronRight } from 'lucide-react'
import Link from 'next/link'
import { usePathname } from 'next/navigation'

import { EndpointLocalityBadge } from '@/components/layout/endpoint-locality-badge'
import { Separator } from '@/components/ui/separator'
import { SidebarTrigger } from '@/components/ui/sidebar'
import { useClasses } from '@/lib/hooks/use-classes'

type Crumb = { label: string; href?: string }

/** The header carries the breadcrumb and the endpoint locality badge, and nothing else. */
export function AppHeader() {
  const pathname = usePathname()
  const { data: classes } = useClasses()

  const crumbs: Crumb[] = [{ label: 'Classes', href: '/' }]
  if (pathname === '/settings') {
    crumbs.push({ label: 'Settings' })
  } else {
    const match = /^\/classes\/(\d+)/.exec(pathname)
    if (match) {
      const classId = Number(match[1])
      const found = classes?.find((item) => item.id === classId)
      crumbs.push({ label: found?.name ?? 'Class' })
    }
  }

  return (
    <header className="sticky top-0 z-10 flex h-14 shrink-0 items-center gap-2 border-b bg-background/85 px-4 supports-[backdrop-filter]:backdrop-blur-sm">
      <SidebarTrigger className="-ml-1" />
      <Separator orientation="vertical" className="mr-1 h-4" />
      <nav aria-label="Breadcrumb" className="min-w-0 flex-1">
        <ol className="flex items-center gap-1.5 text-[13px]">
          {crumbs.map((crumb, index) => (
            <li key={crumb.label} className="flex min-w-0 items-center gap-1.5">
              {index > 0 && (
                <ChevronRight aria-hidden className="size-3.5 shrink-0 text-text-tertiary" />
              )}
              {crumb.href ? (
                <Link
                  href={crumb.href}
                  className="truncate text-text-secondary transition-colors duration-150 hover:text-text-primary"
                >
                  {crumb.label}
                </Link>
              ) : (
                <span aria-current="page" className="truncate font-medium">
                  {crumb.label}
                </span>
              )}
            </li>
          ))}
        </ol>
      </nav>
      <EndpointLocalityBadge />
    </header>
  )
}
