'use client'

import { GraduationCap, Settings } from 'lucide-react'
import Link from 'next/link'
import { usePathname } from 'next/navigation'

import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSkeleton,
  SidebarRail,
  SidebarSeparator,
} from '@/components/ui/sidebar'
import { useClasses } from '@/lib/hooks/use-classes'
import { cn } from '@/lib/utils'

export function AppSidebar() {
  const pathname = usePathname()
  const { data: classes, isPending } = useClasses()

  return (
    <Sidebar variant="inset" collapsible="icon">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton asChild size="lg" tooltip="Lyra home">
              <Link href="/">
                <span className="flex size-8 items-center justify-center rounded-md bg-accent-secondary text-accent-secondary-foreground">
                  <GraduationCap className="size-4" />
                </span>
                <span className="font-heading text-base font-medium tracking-tight">Lyra</span>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarSeparator />

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>Classes</SidebarGroupLabel>
          <SidebarMenu>
            {isPending ? (
              <>
                <SidebarMenuItem>
                  <SidebarMenuSkeleton />
                </SidebarMenuItem>
                <SidebarMenuItem>
                  <SidebarMenuSkeleton />
                </SidebarMenuItem>
              </>
            ) : (
              (classes ?? []).map((item) => {
                const href = `/classes/${item.id}`
                const active = pathname === href
                return (
                  <SidebarMenuItem key={item.id}>
                    <SidebarMenuButton
                      asChild
                      isActive={active}
                      tooltip={item.name}
                      className={cn(
                        active &&
                          'border-l-2 border-accent-primary text-accent-primary font-medium',
                      )}
                    >
                      <Link href={href}>
                        <span className="truncate">{item.code ?? item.name}</span>
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                )
              })
            )}
          </SidebarMenu>
        </SidebarGroup>
      </SidebarContent>

      <SidebarFooter>
        <SidebarSeparator />
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              asChild
              isActive={pathname === '/settings'}
              tooltip="Settings"
              className={cn(
                pathname === '/settings' &&
                  'border-l-2 border-accent-primary text-accent-primary font-medium',
              )}
            >
              <Link href="/settings">
                <Settings />
                <span>Settings</span>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>

      <SidebarRail />
    </Sidebar>
  )
}
