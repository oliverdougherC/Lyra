'use client'

import { useState } from 'react'
import { Archive, MoreVertical, Pencil, Plus, RotateCcw, Trash2 } from 'lucide-react'
import Link from '@/router/link'
import { useRouter } from '@/router/hooks'
import { toast } from 'sonner'

import { HandUnderline } from '@/components/ex-libris'
import { ClassChatsPanel } from '@/components/classes/class-chats-panel'
import { ClassDraftsPanel } from '@/components/classes/class-drafts-panel'
import { ClassFormDialog } from '@/components/classes/class-form-dialog'
import { ClassOverview } from '@/components/classes/class-overview'
import { ClassSolutionsPanel } from '@/components/classes/class-solutions-panel'
import { ClassStudyPanel } from '@/components/classes/class-study-panel'
import { CourseMark } from '@/components/classes/course-mark'
import { DeleteClassDialog } from '@/components/classes/delete-class-dialog'
import { DocumentsPane } from '@/components/documents/documents-pane'
import { ProfileFacts } from '@/components/profile/profile-facts'
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
import { formatRelativeTime } from '@/lib/format'
import { useSessions } from '@/lib/hooks/use-chat'
import { useClass, useUpdateClass } from '@/lib/hooks/use-classes'
import { useDocuments } from '@/lib/hooks/use-documents'
import { useDrafts } from '@/lib/hooks/use-drafts'
import { useClassProfile } from '@/lib/hooks/use-profile'
import { useSolutions } from '@/lib/hooks/use-solutions'
import { useStudyList } from '@/lib/hooks/use-study'
import { cn } from '@/lib/utils'

export const HUB_TABS = [
  'overview',
  'chats',
  'solutions',
  'study',
  'drafts',
  'documents',
  'profile',
] as const

export type HubTab = (typeof HUB_TABS)[number]

export function readHubTab(value: string | null): HubTab {
  return HUB_TABS.includes(value as HubTab) ? (value as HubTab) : 'overview'
}

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
  const { data: study } = useStudyList(classId)
  const { data: drafts } = useDrafts(classId)
  const { data: documents } = useDocuments(classId)
  const { data: profile } = useClassProfile(classId)
  const updateClass = useUpdateClass()

  const [editing, setEditing] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const klass = classQuery.data
  const chatCount = sessions?.length ?? null
  const solutionCount = solutions?.length ?? null
  const studyCount = study ? study.decks.length + study.quizzes.length : null
  const draftCount = drafts?.length ?? null
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
        {/* Scrolls rather than wraps: seven tabs with counts do not fit 375px, and a tab bar
            on two lines stops reading as one control. */}
        <TabsList
          variant="line"
          aria-label="Class sections"
          className="shrink-0 overflow-x-auto overflow-y-hidden"
        >
          <HubTabButton
            value="overview"
            label="Overview"
            count={null}
            active={tab === 'overview'}
          />
          <HubTabButton value="chats" label="Chats" count={chatCount} active={tab === 'chats'} />
          <HubTabButton
            value="solutions"
            label="Solutions"
            count={solutionCount}
            active={tab === 'solutions'}
          />
          <HubTabButton value="study" label="Study" count={studyCount} active={tab === 'study'} />
          <HubTabButton
            value="drafts"
            label="Drafts"
            count={draftCount}
            active={tab === 'drafts'}
          />
          <HubTabButton
            value="documents"
            label="Documents"
            count={documentCount}
            active={tab === 'documents'}
          />
          <HubTabButton
            value="profile"
            label="Profile"
            count={factCount}
            active={tab === 'profile'}
          />
        </TabsList>

        <TabsContent value="overview">
          <ClassOverview classId={classId} className={klass?.name} />
        </TabsContent>

        <TabsContent value="chats">
          <ClassChatsPanel classId={classId} />
        </TabsContent>

        <TabsContent value="solutions">
          <ClassSolutionsPanel classId={classId} />
        </TabsContent>

        <TabsContent value="study">
          <ClassStudyPanel classId={classId} />
        </TabsContent>

        <TabsContent value="drafts">
          <ClassDraftsPanel classId={classId} />
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

/**
 * One section tab. The active tab is marked by the pen: a wobbled hand underline that hugs
 * the word itself, drawn in on selection, excluding the count superscript (design system
 * section 6). The default full-width line underline is suppressed so only the pen marks the
 * place. Counts are printed superscripts and, once their data has loaded, appear on every
 * collection tab including zero, so the strip is consistent rather than counting only some
 * of its tabs (ui-overhaul 3.2).
 */
function HubTabButton({
  value,
  label,
  count,
  active,
}: {
  value: HubTab
  label: string
  count: number | null
  active: boolean
}) {
  return (
    <TabsTrigger value={value} className="after:hidden">
      {/* inline-block, so the absolutely positioned underline has a containing block the
          exact width of the word rather than collapsing to its intrinsic width. */}
      <span className="relative inline-block">
        {label}
        {active ? <HandUnderline /> : null}
      </span>
      {/* A real space before the count, not only the margin: without it the tab announces
          as "Documents17", because JSX drops the newline between the label and this. */}
      {count !== null ? (
        <>
          {' '}
          <span className="text-text-tertiary text-xs tabular-nums">{count}</span>
        </>
      ) : null}
    </TabsTrigger>
  )
}
