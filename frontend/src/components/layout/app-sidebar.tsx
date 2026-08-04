'use client'

import { Suspense, useCallback } from 'react'
import { Archive, ChevronDown, GraduationCap, Plus, RotateCcw, Settings } from 'lucide-react'
import Link from 'next/link'
import { usePathname, useRouter, useSearchParams } from 'next/navigation'

import { CourseMark } from '@/components/classes/course-mark'
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSkeleton,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
  SidebarSeparator,
} from '@/components/ui/sidebar'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { formatSessionFallbackTitle } from '@/lib/format'
import { useClasses, useUpdateClass } from '@/lib/hooks/use-classes'
import { useCreateSession, useSessions } from '@/lib/hooks/use-chat'
import { useLocalStorageState } from '@/lib/hooks/use-local-storage-state'
import { useSolutions } from '@/lib/hooks/use-solutions'
import { cn } from '@/lib/utils'
import type { ClassRead, SolutionRead } from '@/types'

const ARCHIVED_STORAGE_KEY = 'lyra-sidebar-archived-open'

/**
 * The active marker sits inside the row's rounded surface rather than on its border box.
 * A `border-l` lands outside the corner radius and reads as a rule floating beside the
 * row instead of part of it.
 */
const ACTIVE_ROW =
  'relative font-medium text-accent-primary before:absolute before:inset-y-1.5 before:left-0 before:w-0.5 before:rounded-full before:bg-accent-primary'

function parseOpen(raw: string): boolean {
  return raw === 'true'
}

/**
 * `useSearchParams` must sit under a Suspense boundary or statically prerendered pages
 * (the 404 shell) fail the build. The fallback renders the same nav without the active
 * chat highlight, so there is no layout shift.
 */
function SessionParam({
  children,
}: {
  children: (activeSessionId: string | null) => React.ReactNode
}) {
  const searchParams = useSearchParams()
  return children(searchParams.get('session'))
}

/**
 * One class row. Only the class currently open shows its conversations: every other row
 * stays a single line, so a long class list does not bury the workspace in submenus.
 */
function ClassNavItem({
  klass,
  selected,
  activeSessionId,
  sessions,
  sessionsPending,
  solutions,
  activeSolutionId,
  onNewChat,
}: {
  klass: ClassRead
  selected: boolean
  activeSessionId: string | null
  sessions?: { id: number; title: string | null; mode: string; created_at: string }[]
  sessionsPending?: boolean
  solutions?: SolutionRead[]
  activeSolutionId: string | null
  onNewChat: () => void
}) {
  const href = `/classes/${klass.id}`

  return (
    <Collapsible open={selected}>
      <SidebarMenuItem>
        <CollapsibleTrigger asChild>
          <SidebarMenuButton
            asChild
            isActive={selected}
            tooltip={klass.code ? `${klass.code} · ${klass.name}` : klass.name}
            className={cn('h-auto py-1.5', selected && ACTIVE_ROW)}
          >
            <Link href={href} aria-label={klass.code ? `${klass.code}, ${klass.name}` : klass.name}>
              <CourseMark klass={klass} size="sm" />
              <span className="grid min-w-0 flex-1">
                <span className="truncate">{klass.name}</span>
                {klass.code ? (
                  <span className="text-text-tertiary truncate text-xs font-normal">
                    {klass.code}
                  </span>
                ) : null}
              </span>
            </Link>
          </SidebarMenuButton>
        </CollapsibleTrigger>

        {selected ? (
          <SidebarMenuSub>
            {sessionsPending ? (
              <SidebarMenuSubItem aria-busy="true">
                <SidebarMenuSkeleton />
              </SidebarMenuSubItem>
            ) : (
              (sessions ?? []).map((session) => {
                const label = session.title || formatSessionFallbackTitle(session.created_at)
                return (
                  <SidebarMenuSubItem key={session.id}>
                    <SidebarMenuSubButton asChild isActive={activeSessionId === String(session.id)}>
                      <Link href={`${href}?session=${session.id}`} title={label}>
                        <span className="truncate">{label}</span>
                      </Link>
                    </SidebarMenuSubButton>
                  </SidebarMenuSubItem>
                )
              })
            )}
            <SidebarMenuSubItem>
              <SidebarMenuSubButton asChild onClick={onNewChat}>
                <button type="button" aria-label="Start a new chat" className="text-text-secondary">
                  <Plus />
                  <span>New chat</span>
                </button>
              </SidebarMenuSubButton>
            </SidebarMenuSubItem>

            <SidebarMenuSubItem>
              {/* A link rather than a label. The group heading is the most obvious thing
                  to click when you are looking for the solver, and a heading that does
                  nothing sends you back to hunting. */}
              <Link
                href={`${href}/solutions`}
                className="text-text-tertiary hover:text-text-secondary focus-visible:ring-ring mt-2 block rounded-sm px-2 text-[0.6875rem] font-medium tracking-wider uppercase focus-visible:ring-2 focus-visible:outline-none"
              >
                Solutions
              </Link>
            </SidebarMenuSubItem>
            {(solutions ?? []).map((solution) => (
              <SidebarMenuSubItem key={solution.id}>
                <SidebarMenuSubButton asChild isActive={activeSolutionId === String(solution.id)}>
                  <Link href={`${href}/solutions/${solution.id}`} title={solution.title}>
                    <span className="truncate">{solution.title}</span>
                    {/* `awaiting_review` is neither working nor finished, and the student
                        is the thing it is blocked on, so the rail says so. */}
                    {solution.state === 'awaiting_review' ? (
                      <span className="text-info-text ml-auto shrink-0 text-[0.6875rem]">
                        Waiting for you
                      </span>
                    ) : null}
                  </Link>
                </SidebarMenuSubButton>
              </SidebarMenuSubItem>
            ))}
            <SidebarMenuSubItem>
              <SidebarMenuSubButton asChild>
                <Link
                  href={`${href}/solutions/new`}
                  aria-label="Start a new solution set"
                  className="text-text-secondary"
                >
                  <Plus />
                  <span>New solution set</span>
                </Link>
              </SidebarMenuSubButton>
            </SidebarMenuSubItem>
          </SidebarMenuSub>
        ) : null}
      </SidebarMenuItem>
    </Collapsible>
  )
}

export function AppSidebar() {
  const pathname = usePathname()
  const router = useRouter()
  const { data: classes, isPending } = useClasses()
  const updateClass = useUpdateClass()
  const [archivedOpen, setArchivedOpen] = useLocalStorageState(
    ARCHIVED_STORAGE_KEY,
    false,
    parseOpen,
  )

  const classMatch = /^\/classes\/(\d+)/.exec(pathname)
  const selectedClassId = classMatch ? Number(classMatch[1]) : null
  const selectedClassIsValid =
    selectedClassId !== null && Number.isSafeInteger(selectedClassId) && selectedClassId > 0

  const { data: sessions, isPending: sessionsPending } = useSessions(
    selectedClassIsValid ? selectedClassId : null,
  )
  const { data: solutions } = useSolutions(
    selectedClassIsValid ? selectedClassId : Number.NaN,
    selectedClassIsValid,
  )
  const solutionMatch = /^\/classes\/\d+\/solutions\/(\d+)/.exec(pathname)
  const createSession = useCreateSession(selectedClassIsValid ? selectedClassId : null)

  const startNewChat = useCallback(() => {
    if (selectedClassId === null) return
    createSession.mutate(null, {
      onSuccess: (session) => {
        router.push(`/classes/${selectedClassId}?session=${session.id}`)
      },
    })
  }, [createSession, router, selectedClassId])

  const restoreClass = useCallback(
    (classId: number) => {
      updateClass.mutate({ classId, body: { archived: false } })
    },
    [updateClass],
  )

  const allClasses = classes ?? []
  const activeClasses = allClasses.filter((item) => !item.archived)
  const archivedClasses = allClasses.filter((item) => item.archived)

  return (
    <Sidebar variant="inset" collapsible="offcanvas">
      <SidebarHeader className="p-1">
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
              <Suspense
                fallback={activeClasses.map((item) => (
                  <ClassNavItem
                    key={item.id}
                    klass={item}
                    selected={selectedClassId === item.id}
                    activeSessionId={null}
                    solutions={solutions}
                    activeSolutionId={solutionMatch ? solutionMatch[1] : null}
                    sessions={sessions}
                    sessionsPending={sessionsPending}
                    onNewChat={startNewChat}
                  />
                ))}
              >
                <SessionParam>
                  {(activeSessionId) =>
                    activeClasses.map((item) => (
                      <ClassNavItem
                        key={item.id}
                        klass={item}
                        selected={selectedClassId === item.id}
                        activeSessionId={activeSessionId}
                        solutions={solutions}
                        activeSolutionId={solutionMatch ? solutionMatch[1] : null}
                        sessions={sessions}
                        sessionsPending={sessionsPending}
                        onNewChat={startNewChat}
                      />
                    ))
                  }
                </SessionParam>
              </Suspense>
            )}
          </SidebarMenu>
        </SidebarGroup>

        {archivedClasses.length > 0 ? (
          <SidebarGroup>
            <Collapsible open={archivedOpen} onOpenChange={setArchivedOpen}>
              <SidebarGroupLabel asChild>
                <CollapsibleTrigger className="w-full">
                  <span className="flex w-full items-center gap-2">
                    <Archive aria-hidden className="size-3.5" />
                    <span className="flex-1 text-left">Archived</span>
                    <span className="text-text-tertiary tabular-nums">
                      {archivedClasses.length}
                    </span>
                    <ChevronDown
                      aria-hidden
                      className={cn(
                        'text-text-tertiary size-3.5 transition-transform duration-150',
                        !archivedOpen && '-rotate-90',
                      )}
                    />
                  </span>
                </CollapsibleTrigger>
              </SidebarGroupLabel>
              <CollapsibleContent>
                <SidebarMenu>
                  {archivedClasses.map((item) => (
                    <SidebarMenuItem key={item.id}>
                      <SidebarMenuButton
                        asChild
                        isActive={selectedClassId === item.id}
                        tooltip={item.code ? `${item.code} · ${item.name}` : item.name}
                        className={cn(selectedClassId === item.id && ACTIVE_ROW)}
                      >
                        <Link
                          href={`/classes/${item.id}`}
                          aria-label={item.code ? `${item.code}, ${item.name}` : item.name}
                        >
                          <CourseMark klass={item} size="sm" className="opacity-60" />
                          <span className="truncate">{item.name}</span>
                        </Link>
                      </SidebarMenuButton>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <SidebarMenuAction
                            showOnHover
                            onClick={() => restoreClass(item.id)}
                            aria-label={`Move ${item.code ?? item.name} back to active classes`}
                          >
                            <RotateCcw />
                          </SidebarMenuAction>
                        </TooltipTrigger>
                        <TooltipContent>Restore to active classes</TooltipContent>
                      </Tooltip>
                    </SidebarMenuItem>
                  ))}
                </SidebarMenu>
              </CollapsibleContent>
            </Collapsible>
          </SidebarGroup>
        ) : null}
      </SidebarContent>

      <SidebarFooter>
        <SidebarSeparator />
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              asChild
              isActive={pathname === '/settings'}
              tooltip="Settings"
              className={cn(pathname === '/settings' && ACTIVE_ROW)}
            >
              <Link href="/settings">
                <Settings />
                <span>Settings</span>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
    </Sidebar>
  )
}
