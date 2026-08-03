'use client'

import { AppHeader } from '@/components/layout/app-header'
import { AppSidebar } from '@/components/layout/app-sidebar'
import { MobileBottomNav } from '@/components/layout/mobile-bottom-nav'
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar'
import { useLocalStorageState } from '@/lib/hooks/use-local-storage-state'

const SIDEBAR_STORAGE_KEY = 'lyra-sidebar-open'

/**
 * Sidebar widths come from the interface spec (260px expanded, 60px rail) rather than the
 * shadcn defaults.
 */
const SIDEBAR_STYLE = {
  '--sidebar-width': '260px',
  '--sidebar-width-icon': '60px',
} as React.CSSProperties

function parseOpen(raw: string): boolean | null {
  return raw === 'true' ? true : raw === 'false' ? false : null
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useLocalStorageState(SIDEBAR_STORAGE_KEY, true, parseOpen)

  return (
    <SidebarProvider open={open} onOpenChange={setOpen} style={SIDEBAR_STYLE}>
      <a
        href="#main-content"
        className="sr-only rounded-md bg-primary px-4 py-2 font-medium text-primary-foreground focus-visible:not-sr-only focus-visible:absolute focus-visible:top-3 focus-visible:left-3 focus-visible:z-50"
      >
        Skip to content
      </a>
      <AppSidebar />
      <SidebarInset className="min-w-0">
        <AppHeader />
        <main
          id="main-content"
          tabIndex={-1}
          className="mx-auto w-full max-w-[1200px] flex-1 p-4 pb-28 sm:pb-4 md:p-6"
        >
          {children}
        </main>
      </SidebarInset>
      <MobileBottomNav />
    </SidebarProvider>
  )
}
