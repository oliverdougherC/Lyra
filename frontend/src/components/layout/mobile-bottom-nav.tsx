'use client'

import { GraduationCap, Settings } from 'lucide-react'
import Link from 'next/link'
import { usePathname } from 'next/navigation'

import { cn } from '@/lib/utils'

const ITEMS = [
  {
    href: '/',
    label: 'Classes',
    icon: GraduationCap,
    matches: (path: string) => path === '/' || path.startsWith('/classes/'),
  },
  {
    href: '/settings',
    label: 'Settings',
    icon: Settings,
    matches: (path: string) => path === '/settings',
  },
]

/** Compact route navigation for screens below 640px, where a side rail wastes workspace. */
export function MobileBottomNav() {
  const pathname = usePathname()

  return (
    <nav
      aria-label="Mobile navigation"
      className="fixed inset-x-3 bottom-[max(0.75rem,env(safe-area-inset-bottom))] z-20 flex h-16 rounded-md border bg-card shadow-sm sm:hidden"
    >
      {ITEMS.map(({ href, label, icon: Icon, matches }) => {
        const active = matches(pathname)
        return (
          <Link
            key={href}
            href={href}
            aria-current={active ? 'page' : undefined}
            className={cn(
              'flex flex-1 flex-col items-center justify-center gap-1 text-xs font-medium',
              'focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset focus-visible:outline-none',
              active ? 'text-accent-primary' : 'text-text-secondary',
            )}
          >
            <Icon aria-hidden className="size-5" />
            {label}
          </Link>
        )
      })}
    </nav>
  )
}
