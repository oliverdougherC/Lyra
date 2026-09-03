'use client'

import { useState } from 'react'
import { Archive, Info, MoreVertical, Pencil, RotateCcw, Trash2 } from 'lucide-react'
import { useRouter } from '@/router/hooks'
import { toast } from 'sonner'

import { HandUnderline } from '@/components/ex-libris'
import { ClassDetailsSheet } from '@/components/profile/class-details-sheet'
import { ClassFormDialog } from '@/components/classes/class-form-dialog'
import { ClassOverview } from '@/components/classes/class-overview'
import { ClassStudyPanel } from '@/components/classes/class-study-panel'
import { ClassWorkPanel } from '@/components/classes/class-work-panel'
import { CourseMark } from '@/components/classes/course-mark'
import { DeleteClassDialog } from '@/components/classes/delete-class-dialog'
import { DocumentsPane } from '@/components/documents/documents-pane'
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
import { useClassProfile } from '@/lib/hooks/use-profile'
import { useDrafts } from '@/lib/hooks/use-drafts'
import { useSolutions } from '@/lib/hooks/use-solutions'
import { useStudyList } from '@/lib/hooks/use-study'
import { cn } from '@/lib/utils'

/**
 * The four ways into a class, named for the task rather than the subsystem that stores it.
 *
 * `ask` is the front door and the default: the class opens onto the question, not onto a
 * rail of collections. `practice` is the study tools. `work` is everything the student was
 * doing - the conversations, the problem sets, the half-written answers - in one list, so
 * no one has to remember which tab a thing lived under. `files` is the document library.
 * The class facts (the old Profile tab) live in the menu, where the class is the subject.
 */
export const HUB_TABS = ['ask', 'practice', 'work', 'files'] as const

export type HubTab = (typeof HUB_TABS)[number]

/**
 * Old class pages had one tab per subsystem, and bookmarks, history entries, and links
 * from those days still carry those values. Each lands where that collection lives now
 * rather than on the default tab: overview was the front door, study became practice,
 * documents became files, and the profile moved into the class itself.
 */
export const LEGACY_HUB_TABS: Record<string, HubTab> = {
  overview: 'ask',
  chats: 'work',
  solutions: 'work',
  study: 'practice',
  drafts: 'work',
  documents: 'files',
  profile: 'ask',
}

/** The old tabs whose view is one filtered Work list rather than the whole of Work. */
export const LEGACY_HUB_WORK_FILTERS: Record<string, 'chats' | 'solutions' | 'drafts'> = {
  chats: 'chats',
  solutions: 'solutions',
  drafts: 'drafts',
}

export function readHubTab(value: string | null): HubTab {
  if (value === null) return 'ask'
  if (HUB_TABS.includes(value as HubTab)) return value as HubTab
  return LEGACY_HUB_TABS[value] ?? 'ask'
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
 * The hub is one header, four task tabs, and the actions that belong to the class. What
 * used to be a seventh tab (Profile) and a persistent New chat button are gone: the facts
 * are a menu item where the class is the subject, and the question has the front door.
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
  const [detailsOpen, setDetailsOpen] = useState(false)

  const klass = classQuery.data
  const studyCount = study ? study.decks.length + study.quizzes.length : null
  // Work is the getting-back-to-it list: the conversations, the problem sets, the half-
  // written answers, and the study artifacts, in one count.
  const workCount =
    sessions && solutions && drafts && study
      ? sessions.length +
        solutions.length +
        drafts.length +
        study.decks.length +
        study.quizzes.length
      : null
  const documentCount = documents?.length ?? klass?.document_count ?? null
  // Low-confidence facts Lyra has not been confirmed on are the only class facts that
  // still need the student: everything else is read-only. The count is the affordance,
  // the sheet is where the confirming happens.
  const unconfirmedFactCount =
    profile?.facts.filter((fact) => fact.confidence === 'low' && !fact.confirmed && !fact.rejected)
      .length ?? 0

  // The tab lives in the URL, so a class opened on its files is a link the student can
  // send themselves, and Back out of a tab goes where Back should.
  function selectTab(next: string) {
    const target = next === 'ask' ? `/classes/${classId}` : `/classes/${classId}?tab=${next}`
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
    // its own contents: the Files tab hands the height it is given to the file list, and a
    // column sized by its contents would have nothing to hand over.
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
              {unconfirmedFactCount > 0 ? (
                // The one class-level uncertainty that still needs the student. A chip in the
                // subtitle, not a tab: the facts are read silently by every answer, so the
                // nudge rides on the class itself and opens the sheet where they are resolved.
                <button
                  type="button"
                  onClick={() => setDetailsOpen(true)}
                  title="Open the class details to confirm them"
                  className="text-info-text bg-info-fill hover:opacity-80 focus-visible:ring-ring/50 shrink-0 rounded-full px-2 py-0.5 text-xs focus-visible:ring-2 focus-visible:outline-none"
                >
                  {unconfirmedFactCount === 1
                    ? '1 class fact needs confirmation'
                    : `${unconfirmedFactCount} class facts need confirmation`}
                </button>
              ) : null}
            </p>
          </div>
        </div>

        <div className="ml-auto flex shrink-0 items-center gap-2">
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
              <DropdownMenuItem onSelect={() => setDetailsOpen(true)}>
                <Info />
                Class details
              </DropdownMenuItem>
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
        {/* Scrolls rather than wraps: four task tabs with counts must fit 375px, and a tab
            bar on two lines stops reading as one control. */}
        <TabsList
          variant="line"
          aria-label="Class sections"
          className="shrink-0 overflow-x-auto overflow-y-hidden"
        >
          <HubTabButton value="ask" label="Ask" count={null} active={tab === 'ask'} />
          <HubTabButton
            value="practice"
            label="Practice"
            count={studyCount}
            active={tab === 'practice'}
          />
          <HubTabButton value="work" label="Work" count={workCount} active={tab === 'work'} />
          <HubTabButton
            value="files"
            label="Files"
            count={documentCount}
            active={tab === 'files'}
          />
        </TabsList>

        <TabsContent value="ask">
          <ClassOverview classId={classId} className={klass?.name} />
        </TabsContent>

        <TabsContent value="practice">
          <ClassStudyPanel classId={classId} />
        </TabsContent>

        <TabsContent value="work">
          <ClassWorkPanel classId={classId} />
        </TabsContent>

        {/* The one tab that takes the height it is given rather than asking for a height of
            its own. The file list scrolls inside the box and the upload well sits on its
            floor, so the well is on screen at whatever size the window is - it was a fixed
            70svh before, which is a guess that is wrong on both sides: short windows had to
            be scrolled to reach the well, tall ones left a band of empty page beneath it. */}
        <TabsContent value="files" className="flex min-h-0 flex-1 flex-col gap-4">
          <p className="text-text-secondary shrink-0 text-sm">
            Everything Lyra reads for this class. Select files to move or delete them.
          </p>
          <div className="border-border min-h-0 flex-1 overflow-hidden rounded-md border">
            <DocumentsPane classId={classId} variant="manage" />
          </div>
        </TabsContent>
      </Tabs>

      {klass ? (
        <>
          <ClassDetailsSheet
            classId={classId}
            open={detailsOpen}
            onOpenChange={setDetailsOpen}
            klass={klass}
          />
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
          as "Files17", because JSX drops the newline between the label and this. */}
      {count !== null ? (
        <>
          {' '}
          <span className="text-text-tertiary text-xs tabular-nums">{count}</span>
        </>
      ) : null}
    </TabsTrigger>
  )
}
