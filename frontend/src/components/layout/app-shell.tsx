'use client'

import { useMemo } from 'react'
import { useRouter } from 'next/navigation'

import { AppHeader } from '@/components/layout/app-header'
import { AppSidebar } from '@/components/layout/app-sidebar'
import { MobileBottomNav } from '@/components/layout/mobile-bottom-nav'
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar'
import { useAppShortcuts } from '@/lib/hooks/use-app-shortcuts'
import { useLocalStorageState } from '@/lib/hooks/use-local-storage-state'

const SIDEBAR_STORAGE_KEY = 'lyra-sidebar-open'

/**
 * The interface spec uses a 260px expanded sidebar that moves off-canvas when closed.
 */
const SIDEBAR_STYLE = {
  '--sidebar-width': '260px',
} as React.CSSProperties

function parseOpen(raw: string): boolean | null {
  return raw === 'true' ? true : raw === 'false' ? false : null
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useLocalStorageState(SIDEBAR_STORAGE_KEY, true, parseOpen)
  const router = useRouter()

  const shortcuts = useMemo(
    () => [
      // Focusing the composer is the one shortcut worth honouring from inside a field:
      // its whole job is to move focus, including away from another input.
      {
        key: 'k',
        allowInEditable: true,
        run: () => document.getElementById('message-composer')?.focus(),
      },
      { key: ',', run: () => router.push('/settings') },
    ],
    [router],
  )
  useAppShortcuts(shortcuts)

  return (
    <SidebarProvider
      open={open}
      onOpenChange={setOpen}
      style={SIDEBAR_STYLE}
      className="h-svh overflow-hidden"
    >
      <a
        href="#main-content"
        className="sr-only rounded-md bg-primary px-4 py-2 font-medium text-primary-foreground focus-visible:not-sr-only focus-visible:absolute focus-visible:top-3 focus-visible:left-3 focus-visible:z-50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none"
      >
        Skip to content
      </a>
      <AppSidebar />
      <SidebarInset className="min-h-0 min-w-0 overflow-hidden border-border md:peer-data-[variant=inset]:border">
        <AppHeader />
        {/* `main` is the one scroll container below the header, so the rail and header
            stay put on long routes while a full-height route (the workspace) can still
            size itself to exactly what is left. */}
        <main
          id="main-content"
          tabIndex={-1}
          className="flex min-h-0 flex-1 flex-col overflow-y-auto"
        >
          <div className="mx-auto flex min-h-0 w-full max-w-[1320px] flex-1 flex-col p-4 pb-28 sm:pb-4 md:p-6">
            {children}
          </div>
        </main>
      </SidebarInset>
      <MobileBottomNav />
    </SidebarProvider>
  )
}
