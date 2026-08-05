'use client'

import { useState } from 'react'
import {
  Archive,
  ArrowRight,
  FileText,
  MessageSquare,
  MoreVertical,
  Pencil,
  Plus,
  RotateCcw,
  SquareCheckBig,
  Trash2,
  UserRound,
} from 'lucide-react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { toast } from 'sonner'

import { ClassChatsPanel } from '@/components/classes/class-chats-panel'
import { ClassFormDialog } from '@/components/classes/class-form-dialog'
import { ClassSolutionsPanel } from '@/components/classes/class-solutions-panel'
import { CourseMark } from '@/components/classes/course-mark'
import { DeleteClassDialog } from '@/components/classes/delete-class-dialog'
import { DocumentsPane } from '@/components/documents/documents-pane'
import { ProfileFacts } from '@/components/profile/profile-facts'
import { SolutionRow } from '@/components/solutions/solution-row'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { formatCount, formatRelativeTime, formatSessionFallbackTitle } from '@/lib/format'
import { useSessions } from '@/lib/hooks/use-chat'
import { useClass, useUpdateClass } from '@/lib/hooks/use-classes'
import { useDocuments } from '@/lib/hooks/use-documents'
import { useClassProfile } from '@/lib/hooks/use-profile'
import { useSolutions } from '@/lib/hooks/use-solutions'
import { cn } from '@/lib/utils'

export const HUB_TABS = ['overview', 'chats', 'solutions', 'documents', 'profile'] as const

export type HubTab = (typeof HUB_TABS)[number]

export function readHubTab(value: string | null): HubTab {
  return HUB_TABS.includes(value as HubTab) ? (value as HubTab) : 'overview'
}

/** How many rows of each kind the overview shows before it defers to the tab. */
const DIGEST_ROWS = 3

/**
 * The course mark, sized to the two lines it stands beside rather than to the first of
 * them.
 *
 * A fixed square beside a heading and its metadata can only meet one of the two: at `lg`
 * it hung off the title with the line underneath running past its corner. So the mark is
 * exactly as tall as the block, which makes it the block's left edge instead of an icon
 * bolted to the top line.
 *
 * Written as the sum it is rather than as a rounded pixel count: the title's line box, the
 * gap under it, and the metadata's line box. Not `self-stretch` with `aspect-square`, which
 * reads like the right answer and is not - a stretched flex item takes its width from its
 * content before the stretch happens, so the mark came out a narrow rectangle with the
 * initials pressed against its sides.
 */
const MARK_SIZE = [
  // text-3xl (1.875rem) x leading-tight (1.25), + mt-1.5, + text-sm's 1.25rem line box.
  'size-[calc(1.875rem*1.25+0.375rem+1.25rem)]',
  // The same sum with the heading at md:text-4xl (2.25rem).
  'md:size-[calc(2.25rem*1.25+0.375rem+1.25rem)]',
  // The initials grow with the box, or a bigger mark just reads as more empty colour.
  'text-lg md:text-xl',
].join(' ')

/**
 * The class, as a place rather than as a shortcut into one conversation.
 *
 * Clicking a class used to open a chat, which made the class the chat and left everything
 * else - the history, the solution sets, the files, the profile - reachable only through
 * the sidebar, where they could be opened but not managed. This is the room those things
 * live in: one header, one tab bar, and every action that belongs to the class in the
 * place the class is.
 */
export function ClassHub({ classId, tab }: { classId: number; tab: HubTab }) {
  const router = useRouter()
  const classQuery = useClass(classId)
  const { data: sessions } = useSessions(classId)
  const { data: solutions } = useSolutions(classId)
  const { data: documents } = useDocuments(classId)
  const { data: profile } = useClassProfile(classId)
  const updateClass = useUpdateClass()

  const [editing, setEditing] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const klass = classQuery.data
  const chatCount = sessions?.length ?? null
  const solutionCount = solutions?.length ?? null
  const documentCount = documents?.length ?? klass?.document_count ?? null
  const factCount = profile?.facts.length ?? null

  // The tab lives in the URL, so a class opened on its files is a link the student can
  // send themselves, and Back out of a tab goes where Back should.
  function selectTab(next: string) {
    const target = next === 'overview' ? `/classes/${classId}` : `/classes/${classId}?tab=${next}`
    router.replace(target, { scroll: false })
  }

  function onArchiveToggle() {
    if (!klass) return
    updateClass.mutate(
      { classId, body: { archived: !klass.archived } },
      {
        onSuccess: () =>
          toast.success(klass.archived ? `${klass.name} restored.` : `${klass.name} archived.`),
      },
    )
  }

  const subtitle = [klass?.code, klass?.semester].filter(Boolean).join(' · ')

  return (
    // `min-h-0 flex-1` so the column can be measured against the window rather than against
    // its own contents: the Documents tab hands the height it is given to the file list, and
    // a column sized by its contents would have nothing to hand over.
    <div className="mx-auto flex min-h-0 w-full max-w-4xl flex-1 flex-col gap-6 pt-2 md:pt-6">
      <header className="flex flex-wrap items-start gap-4">
        {/* The mark and the words it names are one block, kept apart from the actions so
            they wrap as a unit on a narrow screen. */}
        {/* `min-w-[14rem]` rather than `min-w-0`: below that width the actions wrap to a
            line of their own instead of squeezing the class name down to an ellipsis, which
            is what a 375px screen was doing to every course whose name is longer than a
            word. */}
        <div className="flex min-w-[14rem] flex-1 items-start gap-4">
          {klass ? (
            <CourseMark klass={klass} size="lg" className={MARK_SIZE} />
          ) : (
            <Skeleton className={cn('shrink-0 rounded-md', MARK_SIZE)} />
          )}

          <div className="min-w-0 flex-1">
            {klass ? (
              <h1 className="font-display text-3xl leading-tight text-pretty md:text-4xl">
                {klass.name}
              </h1>
            ) : (
              <Skeleton className="h-9 w-64" />
            )}
            {/* One string with its own separators, not a row of spans: as a flex row the
                middle dot wrapped onto a line by itself whenever the term and the activity
                did not both fit. */}
            <p className="text-text-tertiary mt-1.5 flex flex-wrap items-center gap-2 text-sm">
              {klass ? (
                <span>
                  {[subtitle, `Active ${formatRelativeTime(klass.last_active_at)}`]
                    .filter(Boolean)
                    .join(' · ')}
                </span>
              ) : null}
              {klass?.archived ? (
                <span className="text-text-tertiary border-border rounded-full border px-2 py-0.5 text-xs">
                  Archived
                </span>
              ) : null}
            </p>
          </div>
        </div>

        <div className="ml-auto flex shrink-0 items-center gap-2">
          <Button asChild size="sm">
            <Link href={`/classes/${classId}/chat?session=new`}>
              <Plus className="size-4" />
              New chat
            </Link>
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="size-8"
                aria-label={`Actions for ${klass?.name ?? 'this class'}`}
              >
                <MoreVertical />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={() => setEditing(true)}>
                <Pencil />
                Rename or edit
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={onArchiveToggle}>
                {klass?.archived ? <RotateCcw /> : <Archive />}
                {klass?.archived ? 'Restore' : 'Archive'}
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem variant="destructive" onSelect={() => setDeleting(true)}>
                <Trash2 />
                Delete class
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </header>

      <Tabs value={tab} onValueChange={selectTab} className="min-h-0 flex-1 gap-6">
        {/* Scrolls rather than wraps: five tabs with counts do not fit 375px, and a tab bar
            on two lines stops reading as one control. */}
        <TabsList
          variant="line"
          aria-label="Class sections"
          className="shrink-0 overflow-x-auto overflow-y-hidden"
        >
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="chats">
            Chats
            <TabCount value={chatCount} />
          </TabsTrigger>
          <TabsTrigger value="solutions">
            Solutions
            <TabCount value={solutionCount} />
          </TabsTrigger>
          <TabsTrigger value="documents">
            Documents
            <TabCount value={documentCount} />
          </TabsTrigger>
          <TabsTrigger value="profile">
            Profile
            <TabCount value={factCount} />
          </TabsTrigger>
        </TabsList>

        <TabsContent value="overview">
          <HubOverview
            classId={classId}
            chatCount={chatCount}
            solutionCount={solutionCount}
            documentCount={documentCount}
            factCount={factCount}
            onSelectTab={selectTab}
          />
        </TabsContent>

        <TabsContent value="chats">
          <ClassChatsPanel classId={classId} />
        </TabsContent>

        <TabsContent value="solutions">
          <ClassSolutionsPanel classId={classId} />
        </TabsContent>

        {/* The one tab that takes the height it is given rather than asking for a height of
            its own. The file list scrolls inside the box and the upload well sits on its
            floor, so the well is on screen at whatever size the window is - it was a fixed
            70svh before, which is a guess that is wrong on both sides: short windows had to
            be scrolled to reach the well, tall ones left a band of empty page beneath it. */}
        <TabsContent value="documents" className="flex min-h-0 flex-1 flex-col gap-4">
          <p className="text-text-secondary shrink-0 text-sm">
            Everything Lyra reads for this class. Select files to move or delete them.
          </p>
          <div className="border-border min-h-0 flex-1 overflow-hidden rounded-md border">
            <DocumentsPane classId={classId} variant="manage" />
          </div>
        </TabsContent>

        <TabsContent value="profile">
          <div className="flex flex-col gap-4">
            <p className="text-text-secondary text-sm">
              What Lyra has worked out about this class from everything you uploaded. Click any
              value to correct it.
            </p>
            <ProfileFacts classId={classId} />
          </div>
        </TabsContent>
      </Tabs>

      {klass ? (
        <>
          <ClassFormDialog open={editing} onOpenChange={setEditing} klass={klass} />
          <DeleteClassDialog
            klass={deleting ? klass : null}
            onOpenChange={(open) => {
              if (!open) setDeleting(false)
            }}
            // The page is the class, so once it is gone there is nothing here to stand on.
            onDeleted={() => router.push('/')}
          />
        </>
      ) : null}
    </div>
  )
}

function TabCount({ value }: { value: number | null }) {
  if (value === null || value === 0) return null
  return (
    <>
      {/* A real space, not only the margin: without it the tab announces as
          "Documents17", because JSX drops the newline between the label and this. */}{' '}
      <span className="text-text-tertiary text-xs tabular-nums">{value}</span>
    </>
  )
}

function HubOverview({
  classId,
  chatCount,
  solutionCount,
  documentCount,
  factCount,
  onSelectTab,
}: {
  classId: number
  chatCount: number | null
  solutionCount: number | null
  documentCount: number | null
  factCount: number | null
  onSelectTab: (tab: HubTab) => void
}) {
  const { data: sessions } = useSessions(classId)
  const { data: solutions } = useSolutions(classId)
  const { data: documents } = useDocuments(classId)
  const { data: profile } = useClassProfile(classId)

  const unconfirmed =
    profile?.facts.filter((fact) => fact.confidence === 'low' && !fact.confirmed && !fact.rejected)
      .length ?? 0

  return (
    <div className="flex flex-col gap-8">
      <HubSection
        title="Conversations"
        icon={<MessageSquare aria-hidden className="size-4" />}
        count={chatCount}
        empty="Nothing asked yet."
        action={{ label: 'New chat', href: `/classes/${classId}/chat?session=new` }}
        onViewAll={chatCount ? () => onSelectTab('chats') : undefined}
      >
        {(sessions ?? []).slice(0, DIGEST_ROWS).map((session) => (
          <li key={session.id}>
            <Link
              href={`/classes/${classId}/chat?session=${session.id}`}
              className="hover:bg-muted focus-visible:ring-ring flex items-center gap-3 rounded-md px-3 py-2 transition-colors focus-visible:ring-2 focus-visible:outline-none"
            >
              <span className="min-w-0 flex-1 truncate text-sm">
                {session.title || formatSessionFallbackTitle(session.created_at)}
              </span>
              <span className="text-text-tertiary shrink-0 text-xs">
                {formatRelativeTime(session.created_at)}
              </span>
            </Link>
          </li>
        ))}
      </HubSection>

      <HubSection
        title="Solution sets"
        icon={<SquareCheckBig aria-hidden className="size-4" />}
        count={solutionCount}
        empty="No problem sets solved yet."
        action={{ label: 'New solution set', href: `/classes/${classId}/solutions/new` }}
        onViewAll={solutionCount ? () => onSelectTab('solutions') : undefined}
      >
        {(solutions ?? []).slice(0, DIGEST_ROWS).map((solution) => (
          <li key={solution.id}>
            <SolutionRow classId={classId} solution={solution} />
          </li>
        ))}
      </HubSection>

      <HubSection
        title="Documents"
        icon={<FileText aria-hidden className="size-4" />}
        count={documentCount}
        empty="Nothing uploaded yet."
        action={{ label: 'Manage files', onClick: () => onSelectTab('documents') }}
        onViewAll={documentCount ? () => onSelectTab('documents') : undefined}
      >
        {(documents ?? []).slice(0, DIGEST_ROWS + 1).map((document) => (
          <li key={document.id} className="flex items-center gap-3 rounded-md px-3 py-2 text-sm">
            <FileText aria-hidden className="text-text-tertiary size-4 shrink-0" />
            <span className="min-w-0 flex-1 truncate" title={document.filename}>
              {document.filename}
            </span>
            <span
              className={cn(
                'shrink-0 text-xs',
                document.state === 'failed' ? 'text-danger-text' : 'text-text-tertiary',
              )}
            >
              {document.state === 'ready' ? 'Indexed' : document.state}
            </span>
          </li>
        ))}
      </HubSection>

      <HubSection
        title="Class profile"
        icon={<UserRound aria-hidden className="size-4" />}
        count={factCount}
        empty="Upload a syllabus and Lyra will fill this in."
        action={{ label: 'Open profile', onClick: () => onSelectTab('profile') }}
      >
        {factCount ? (
          <li className="text-text-secondary px-3 py-2 text-sm">
            {formatCount(factCount, 'fact')} about this class.
            {unconfirmed > 0 ? (
              <span className="text-info-text">
                {' '}
                {unconfirmed === 1 ? 'One still needs' : `${unconfirmed} still need`} your
                confirmation.
              </span>
            ) : null}
          </li>
        ) : null}
      </HubSection>
    </div>
  )
}

function HubSection({
  title,
  icon,
  count,
  empty,
  action,
  onViewAll,
  children,
}: {
  title: string
  icon: React.ReactNode
  count: number | null
  empty: string
  action: { label: string; href?: string; onClick?: () => void }
  onViewAll?: () => void
  children: React.ReactNode
}) {
  const hasRows = Array.isArray(children) ? children.length > 0 : Boolean(children)

  return (
    <section className="flex flex-col gap-3">
      <div className="border-border/70 flex items-center gap-2 border-b pb-2">
        <span className="text-text-tertiary">{icon}</span>
        <h2 className="text-xs font-medium tracking-[0.14em] uppercase">{title}</h2>
        {count ? <span className="text-text-tertiary text-xs tabular-nums">{count}</span> : null}
        {action.href ? (
          <Button asChild variant="ghost" size="sm" className="ml-auto h-7">
            <Link href={action.href}>{action.label}</Link>
          </Button>
        ) : (
          <Button variant="ghost" size="sm" className="ml-auto h-7" onClick={action.onClick}>
            {action.label}
          </Button>
        )}
      </div>

      {hasRows ? (
        <ul className="flex flex-col gap-1">{children}</ul>
      ) : (
        <p className="text-text-tertiary px-3 py-2 text-sm">{empty}</p>
      )}

      {onViewAll ? (
        <button
          type="button"
          onClick={onViewAll}
          className="text-text-secondary hover:text-accent-primary focus-visible:ring-ring inline-flex items-center gap-1.5 self-start rounded-sm px-3 text-xs transition-colors focus-visible:ring-2 focus-visible:outline-none"
        >
          View all
          <ArrowRight aria-hidden className="size-3" />
        </button>
      ) : null}
    </section>
  )
}
