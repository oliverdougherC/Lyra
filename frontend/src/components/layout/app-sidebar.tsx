'use client'

import { Suspense, useCallback, useState } from 'react'
import { Archive, ChevronDown, Moon, RotateCcw, Settings, Sun } from 'lucide-react'
import Link from '@/router/link'
import { usePathname, useSearchParams } from '@/router/hooks'

import { CourseMark } from '@/components/classes/course-mark'
import { LyraMark } from '@/components/chat/lyra-mark'
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
import { useSessions } from '@/lib/hooks/use-chat'
import { useDrafts } from '@/lib/hooks/use-drafts'
import { useLocalStorageState } from '@/lib/hooks/use-local-storage-state'
import { useSolutions } from '@/lib/hooks/use-solutions'
import { useStudyList } from '@/lib/hooks/use-study'
import { useTheme } from '@/lib/theme'
import { cn } from '@/lib/utils'
import type { ClassRead, DraftRead, SolutionRead, StudyListRead } from '@/types'

const ARCHIVED_STORAGE_KEY = 'lyra-sidebar-archived-open'

/**
 * How many conversations a class shows before the rest fold away.
 *
 * A term's worth of chats is a hundred rows, and a rail that long buries Solutions and
 * everything under it. Five is enough to cover "the one I was just in" without the list
 * becoming the sidebar.
 */
const VISIBLE_SESSIONS = 5

/**
 * How many pieces of work a class shows under its conversations.
 *
 * One list across solution sets, drafts, decks, and quizzes, ordered by each artifact's
 * `updated_at`. That timestamp moves when the work changed (a pass finished, a body
 * autosaved), which is close to but not the same as when the student last opened it, so
 * the group is headed "Work", not "Recent". The rail used to list only solutions, which
 * made the solver the one kind of work you could get back to from anywhere; the
 * student's mental model is "the thing I was doing", not "which subsystem owns the thing
 * I was doing".
 */
const VISIBLE_WORK = 4

/** One piece of work in the rail, whatever subsystem owns it. */
type WorkItem = {
  key: string
  href: string
  title: string
  /** A quiet note when the row needs the student or is still moving. */
  note: string | null
  noteTone: 'info' | 'nominal'
  updatedAt: string
}

function workItems(
  classHref: string,
  solutions: SolutionRead[] | undefined,
  drafts: DraftRead[] | undefined,
  study: StudyListRead | undefined,
): WorkItem[] {
  const items: WorkItem[] = []

  for (const solution of solutions ?? []) {
    items.push({
      key: `solution-${solution.id}`,
      href: `${classHref}/solutions/${solution.id}`,
      title: solution.title,
      // `awaiting_review` is neither working nor finished, and the student is the thing
      // it is blocked on, so the rail says so.
      note:
        solution.state === 'awaiting_review'
          ? 'Waiting for you'
          : solution.state === 'solving' || solution.state === 'segmenting'
            ? 'Working'
            : null,
      noteTone: solution.state === 'awaiting_review' ? 'info' : 'nominal',
      updatedAt: solution.updated_at,
    })
  }

  for (const draft of drafts ?? []) {
    items.push({
      key: `draft-${draft.id}`,
      href: `${classHref}/drafts/${draft.id}`,
      title: draft.title,
      note: draft.state === 'generating' || draft.state === 'pending' ? 'Working' : null,
      noteTone: 'nominal',
      updatedAt: draft.updated_at,
    })
  }

  for (const deck of study?.decks ?? []) {
    items.push({
      key: `study-${deck.id}`,
      href: `${classHref}/study/${deck.id}`,
      title: deck.title,
      note:
        deck.state === 'ready' && deck.due_count > 0
          ? `${deck.due_count} due`
          : deck.state === 'generating' || deck.state === 'pending'
            ? 'Working'
            : null,
      noteTone: deck.state === 'ready' && deck.due_count > 0 ? 'info' : 'nominal',
      updatedAt: deck.updated_at,
    })
  }

  for (const quiz of study?.quizzes ?? []) {
    items.push({
      key: `study-${quiz.id}`,
      href: `${classHref}/study/${quiz.id}`,
      title: quiz.title,
      note: quiz.state === 'generating' || quiz.state === 'pending' ? 'Working' : null,
      noteTone: 'nominal',
      updatedAt: quiz.updated_at,
    })
  }

  return items.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)).slice(0, VISIBLE_WORK)
}

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

type SessionSummary = { id: number; title: string | null; mode: string; created_at: string }

function SessionSubItem({
  session,
  href,
  activeSessionId,
}: {
  session: SessionSummary
  href: string
  activeSessionId: string | null
}) {
  const label = session.title || formatSessionFallbackTitle(session.created_at)
  return (
    <SidebarMenuSubItem>
      <SidebarMenuSubButton asChild isActive={activeSessionId === String(session.id)}>
        <Link href={`${href}/chat?session=${session.id}`} title={label}>
          <span className="truncate">{label}</span>
        </Link>
      </SidebarMenuSubButton>
    </SidebarMenuSubItem>
  )
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
  drafts,
  study,
  activeWorkPath,
}: {
  klass: ClassRead
  selected: boolean
  activeSessionId: string | null
  sessions?: SessionSummary[]
  sessionsPending?: boolean
  solutions?: SolutionRead[]
  drafts?: DraftRead[]
  study?: StudyListRead
  /** The pathname, for marking the work row currently open. */
  activeWorkPath: string
}) {
  const href = `/classes/${klass.id}`
  const [showAllSessions, setShowAllSessions] = useState(false)
  const recentWork = workItems(href, solutions, drafts, study)

  const allSessions = sessions ?? []
  const headSessions = allSessions.slice(0, VISIBLE_SESSIONS)
  const restSessions = allSessions.slice(VISIBLE_SESSIONS)
  // The conversation being read is always on the list, wherever it sits in the history.
  // Folding it away would leave the rail with no highlighted row and no way back to it.
  const pinnedSessions = showAllSessions
    ? []
    : restSessions.filter((session) => activeSessionId === String(session.id))
  // The rest open *below* the toggle. Growing the list upward would shove the control
  // you just clicked off under the cursor, so collapsing again means hunting for it.
  const tailSessions = showAllSessions ? restSessions : []
  const hiddenCount = restSessions.length - pinnedSessions.length - tailSessions.length

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
            {/* The rail is navigation, never verbs (ui-overhaul 2.2): it lists a class's
                recent destinations - conversations and solution sets - and the hub owns the
                actions that create them. */}
            {sessionsPending ? (
              <SidebarMenuSubItem aria-busy="true">
                <SidebarMenuSkeleton />
              </SidebarMenuSubItem>
            ) : (
              [...headSessions, ...pinnedSessions].map((session) => (
                <SessionSubItem
                  key={session.id}
                  session={session}
                  href={href}
                  activeSessionId={activeSessionId}
                />
              ))
            )}
            {hiddenCount > 0 || showAllSessions ? (
              <SidebarMenuSubItem>
                <SidebarMenuSubButton asChild onClick={() => setShowAllSessions(!showAllSessions)}>
                  <button
                    type="button"
                    aria-expanded={showAllSessions}
                    className="text-text-tertiary"
                  >
                    <ChevronDown
                      aria-hidden
                      className={cn(
                        'transition-transform duration-150',
                        !showAllSessions && '-rotate-90',
                      )}
                    />
                    <span>{showAllSessions ? 'Show fewer' : `Show all ${allSessions.length}`}</span>
                  </button>
                </SidebarMenuSubButton>
              </SidebarMenuSubItem>
            ) : null}
            {tailSessions.map((session) => (
              <SessionSubItem
                key={session.id}
                session={session}
                href={href}
                activeSessionId={activeSessionId}
              />
            ))}

            {recentWork.length > 0 ? (
              <SidebarMenuSubItem>
                {/* A link rather than a label. The group heading is the most obvious thing
                    to click when you are looking for your work, and a heading that does
                    nothing sends you back to hunting. It opens the class, which is where
                    every kind of work is browsed and managed. */}
                <Link
                  href={href}
                  className="eyebrow hover:text-text-secondary focus-visible:ring-ring mt-2 block rounded-sm px-2 transition-colors duration-150 focus-visible:ring-2 focus-visible:outline-none"
                >
                  Work
                </Link>
              </SidebarMenuSubItem>
            ) : null}
            {recentWork.map((item) => (
              <SidebarMenuSubItem key={item.key}>
                <SidebarMenuSubButton asChild isActive={activeWorkPath === item.href}>
                  <Link href={item.href} title={item.title}>
                    <span className="truncate">{item.title}</span>
                    {item.note ? (
                      <span
                        className={cn(
                          'ml-auto shrink-0 text-[0.6875rem]',
                          item.noteTone === 'info' ? 'text-info-text' : 'text-text-tertiary',
                        )}
                      >
                        {item.note}
                      </span>
                    ) : null}
                  </Link>
                </SidebarMenuSubButton>
              </SidebarMenuSubItem>
            ))}
          </SidebarMenuSub>
        ) : null}
      </SidebarMenuItem>
    </Collapsible>
  )
}

/**
 * The reading-room / after-hours quick-toggle. Two states, one tap; the icon and label name
 * the mode it switches to, so the control says what it does rather than what is on.
 */
function ModeToggle() {
  const { resolvedTheme, setTheme } = useTheme()
  const isDark = resolvedTheme === 'dark'
  return (
    <SidebarMenuButton
      onClick={() => setTheme(isDark ? 'light' : 'dark')}
      tooltip={isDark ? 'Switch to the reading room' : 'Switch to after hours'}
    >
      {isDark ? <Sun /> : <Moon />}
      <span>{isDark ? 'Reading room' : 'After hours'}</span>
    </SidebarMenuButton>
  )
}

export function AppSidebar() {
  const pathname = usePathname()
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
  const { data: drafts } = useDrafts(
    selectedClassIsValid ? selectedClassId : Number.NaN,
    selectedClassIsValid,
  )
  const { data: study } = useStudyList(
    selectedClassIsValid ? selectedClassId : Number.NaN,
    selectedClassIsValid,
  )

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
    // `sidebar`, not `inset`. The inset variant floats the whole application inside a
    // rounded, bordered, shadowed panel — a card, and the largest one in the product. The
    // app is one continuous surface: a raised paper rail flush against the canvas.
    <Sidebar variant="sidebar" collapsible="offcanvas">
      {/* `px-2 py-1`, not `p-1`: the horizontal padding puts the mark on the same column as
          the class marks below, while the vertical padding keeps the header 56px so its rule
          still meets the app header's border. */}
      {/* The same 56px box as the app header, closed by its own bottom border rather than
          a separator sitting underneath it. A separator is a sibling *below* the box, so
          its hairline landed 1px lower than the header's border-bottom and the two halves
          of what should be one rule were offset by a pixel. */}
      <SidebarHeader className="h-14 shrink-0 justify-center border-b px-2 py-0">
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton asChild size="lg" tooltip="Lyra home">
              <Link href="/">
                {/* The product's own mark, not a stock icon: the same star that thinks
                    beside every answer is the thing that names the app. Sized `!`, because
                    the menu button shrinks every descendant svg to an icon's 16px and the
                    wordmark's own mark is not an icon — at 16px its ink reads smaller than
                    the capital it stands beside. 24px puts the orbit at the word's own ink
                    height, and centred on the row it sits on the cap's optical centre. */}
                <LyraMark className="text-accent-primary size-6!" />
                <span className="font-wordmark text-text-primary text-[1.2rem] font-medium">
                  Lyra
                </span>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel className="eyebrow">Classes</SidebarGroupLabel>
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
                    drafts={drafts}
                    study={study}
                    activeWorkPath={pathname}
                    sessions={sessions}
                    sessionsPending={sessionsPending}
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
                        drafts={drafts}
                        study={study}
                        activeWorkPath={pathname}
                        sessions={sessions}
                        sessionsPending={sessionsPending}
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
                    <Archive aria-hidden className="size-3" />
                    <span className="eyebrow flex-1 text-left">Archived</span>
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
          {/* The mode quick-toggle lives at the foot of the rail (design system section 8);
              the full three-way control, with system, stays in Settings. */}
          <SidebarMenuItem>
            <ModeToggle />
          </SidebarMenuItem>
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
