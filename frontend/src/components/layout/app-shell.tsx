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
      {/* `overflow-clip` rather than `overflow-hidden`. Both clip the same pixels, but
          `hidden` also makes the box a scroll container, and this one is positioned, so
          it was the containing block for any absolutely positioned descendant that had
          no positioned parent of its own — every `sr-only` form control, among others.
          Focusing one made the browser scroll a box with no scrollbar and no wheel
          handling, which read to the user as the page disappearing. `clip` cannot
          scroll, so that failure is now unreachable rather than merely unlikely. */}
      <SidebarInset className="min-h-0 min-w-0 overflow-clip border-border md:peer-data-[variant=inset]:border">
        <AppHeader />
        {/* `main` is the one scroll container below the header, so the rail and header
            stay put on long routes while a full-height route (the workspace) can still
            size itself to exactly what is left. It is `relative` so that it, and not
            something further up, is the containing block for absolutely positioned
            content: whatever the browser decides to scroll into view should be the box
            the student can actually scroll. */}
        <main
          id="main-content"
          tabIndex={-1}
          className="relative flex min-h-0 flex-1 flex-col overflow-y-auto"
        >
          <div className="mx-auto flex min-h-0 w-full max-w-[1320px] flex-1 flex-col p-4 pb-0 md:p-6 md:pb-0">
            {children}
            {/* Breathing room as a spacer rather than as padding on this box.
                `flex-1` is `1 1 0%`, so this box is always exactly the scroll container's
                height and a long page's content overflows it; padding here would sit above
                that overflow and contribute nothing at the end of the scroll, leaving the
                last button flush against the window edge however much padding was set. A
                sibling that cannot shrink is part of the content in both cases: it ends
                the scroll on a long page, and on a full-height route it simply lifts the
                workbench off the edge.

                Its height matches this box's side padding at every breakpoint, so the
                page is inset by the same amount all the way round. Only the mobile size
                differs, and only because the bottom nav is sitting there. */}
            <div aria-hidden className="h-28 shrink-0 sm:h-4 md:h-6 print:hidden" />
          </div>
        </main>
      </SidebarInset>
      <MobileBottomNav />
    </SidebarProvider>
  )
}
