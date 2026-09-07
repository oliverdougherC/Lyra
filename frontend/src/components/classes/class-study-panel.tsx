'use client'

import { useId, useState } from 'react'
import {
  ChevronRight,
  FileWarning,
  Layers,
  ListChecks,
  MoreVertical,
  Pencil,
  Plus,
  Trash2,
} from 'lucide-react'
import Link from '@/router/link'
import { useRouter } from '@/router/hooks'
import { toast } from 'sonner'

import { RenameDialog } from '@/components/classes/rename-dialog'
import { SourcePicker } from '@/components/solutions/source-picker'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '@/components/ui/spinner'
import { ApiError } from '@/lib/api'
import { formatCount, formatRelativeTime } from '@/lib/format'
import { quickStudyTitle } from '@/lib/handoff'
import { useDocuments } from '@/lib/hooks/use-documents'
import { cn } from '@/lib/utils'
import {
  useCreateDeck,
  useCreateQuiz,
  useDeleteDeck,
  useDeleteQuiz,
  useRenameDeck,
  useRenameQuiz,
  useStudyList,
} from '@/lib/hooks/use-study'
import type {
  ArtifactState,
  DeckSummary,
  QuizDifficulty,
  QuizQuestionType,
  StudyArtifactRead,
} from '@/types'

/** Where a study run has got to, in words rather than internal state names. */
const STUDY_STATE_LABELS: Partial<Record<ArtifactState, string>> = {
  pending: 'Queued',
  generating: 'Generating',
  ready: 'Ready',
  failed: 'Could not finish',
}

function stateLabel(state: ArtifactState): string {
  return STUDY_STATE_LABELS[state] ?? state
}

/** The counts, said only when they are real; the stage line while Lyra is writing. */
function describeDeck(deck: DeckSummary): string {
  if (deck.state === 'pending' || deck.state === 'generating') {
    return deck.stage_detail ?? stateLabel(deck.state)
  }
  return formatCount(deck.cards_total, 'card')
}

function describeQuiz(quiz: StudyArtifactRead): string {
  if (quiz.state === 'pending' || quiz.state === 'generating') {
    return quiz.stage_detail ?? stateLabel(quiz.state)
  }
  const size = formatCount(quiz.problems_total ?? 0, 'question')
  return quiz.state === 'ready' &&
    quiz.active_attempt_id != null &&
    quiz.answered_count !== undefined
    ? `${size} · ${quiz.answered_count} answered`
    : size
}

/** A deck or quiz being renamed or deleted; the kind picks the mutation. */
type StudyTarget = { kind: 'deck' | 'quiz'; id: number; title: string }

/** Every deck and quiz in a class, with the actions the solutions panel taught. */
export function ClassStudyPanel({ classId }: { classId: number }) {
  return <StudyList key={classId} classId={classId} />
}

function StudyList({ classId }: { classId: number }) {
  const storageKey = `lyra:class:${classId}:practice-list`
  const [view, setView] = useState(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey) ?? '{}')
      return {
        query: typeof saved.query === 'string' ? saved.query : '',
        limit: Number.isInteger(saved.limit) && saved.limit >= 20 ? saved.limit : 20,
      }
    } catch {
      return { query: '', limit: 20 }
    }
  })
  function updateView(next: typeof view) {
    setView(next)
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(next))
    } catch {
      /* Browsing still works without storage. */
    }
  }
  const router = useRouter()
  const study = useStudyList(classId)
  const documents = useDocuments(classId)
  const createQuiz = useCreateQuiz(classId)
  const renameDeck = useRenameDeck(classId)
  const renameQuiz = useRenameQuiz(classId)
  const deleteDeck = useDeleteDeck(classId)
  const deleteQuiz = useDeleteQuiz(classId)
  const [creating, setCreating] = useState<'deck' | 'quiz' | null>(null)
  const [renaming, setRenaming] = useState<StudyTarget | null>(null)
  const [deleting, setDeleting] = useState<StudyTarget | null>(null)

  const documentsLoaded = documents.data !== undefined && !documents.isError
  const readyCount = (documents.data ?? []).filter((item) => item.state === 'ready').length
  // Every name already taken by a deck or a quiz, so a second quick create inside the
  // same minute gets numbered instead of a twin.
  const existingTitles = [...(study.data?.decks ?? []), ...(study.data?.quizzes ?? [])].map(
    (artifact) => artifact.title,
  )

  /**
   * The one-click way in: a quiz from everything ready, named to the minute, at the
   * defaults the dialog would have offered anyway. The dialog remains for anyone who
   * wants to choose sources, counts, or difficulty; nobody has to visit it to start
   * studying.
   *
   * Disabled until the document list has loaded: while the query is pending the ready
   * count is not zero, it is unknown, and a fast click must not be told "nothing is
   * ready" by a screen that has not found out yet.
   */
  function startPractice() {
    if (readyCount === 0) {
      toast.error('No documents are ready to practice from yet.')
      return
    }
    createQuiz.mutate(
      { title: quickStudyTitle('quiz', existingTitles) },
      {
        onSuccess: (artifact) => router.push(`/classes/${classId}/study/${artifact.id}`),
        onError: (error) =>
          toast.error(
            error instanceof ApiError ? error.message : 'Could not start a practice set.',
          ),
      },
    )
  }

  async function onConfirmDelete() {
    if (!deleting) return
    const mutation = deleting.kind === 'deck' ? deleteDeck : deleteQuiz
    try {
      await mutation.mutateAsync(deleting.id)
      toast.success(`${deleting.title} deleted.`)
      setDeleting(null)
    } catch (caught) {
      toast.error(caught instanceof ApiError ? caught.message : 'Could not delete that study tool.')
    }
  }

  if (study.isPending) {
    return (
      <div className="flex flex-col gap-2" aria-busy="true" aria-label="Loading study tools">
        {[0, 1, 2].map((row) => (
          <Skeleton key={row} className="h-16 w-full rounded-md" />
        ))}
      </div>
    )
  }

  if (study.isError && !study.data) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load your study tools</AlertTitle>
        <AlertDescription>
          <p>{study.error instanceof ApiError ? study.error.message : 'Something went wrong.'}</p>
          <Button
            variant="outline"
            size="sm"
            className="mt-3"
            disabled={study.isFetching}
            onClick={() => void study.refetch()}
          >
            {study.isFetching ? 'Retrying…' : 'Retry'}
          </Button>
        </AlertDescription>
      </Alert>
    )
  }

  const { decks, quizzes } = study.data!
  const empty = decks.length === 0 && quizzes.length === 0
  const matches = [...decks, ...quizzes]
    .filter((item) =>
      item.title.toLocaleLowerCase().includes(view.query.trim().toLocaleLowerCase()),
    )
    .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime())
  const visibleIds = new Set(matches.slice(0, view.limit).map((item) => item.id))
  const visibleDecks = decks.filter((item) => visibleIds.has(item.id))
  const visibleQuizzes = quizzes.filter((item) => visibleIds.has(item.id))
  const dueDeck = decks.find((deck) => deck.state === 'ready' && deck.due_count > 0)
  const activeQuiz = quizzes.find(
    (quiz) => quiz.state === 'ready' && quiz.active_attempt_id != null,
  )
  const recommended = dueDeck ?? activeQuiz

  return (
    <div className="flex flex-col gap-6">
      {study.isError ? (
        <Alert variant="destructive">
          <AlertTitle>Could not refresh your study tools</AlertTitle>
          <AlertDescription>
            Previously loaded study tools remain available.
            <Button
              variant="outline"
              size="sm"
              disabled={study.isFetching}
              onClick={() => void study.refetch()}
            >
              {study.isFetching ? 'Retrying…' : 'Retry study tools'}
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}
      {recommended ? (
        <div className="flex flex-wrap items-center gap-3">
          <Button
            onClick={() =>
              router.push(
                `/classes/${classId}/study/${recommended.id}${dueDeck ? '?review=due' : ''}`,
              )
            }
          >
            {dueDeck ? 'Review due' : 'Continue quiz'}
          </Button>
          <span className="min-w-0 flex-1 break-words text-sm">{recommended.title}</span>
        </div>
      ) : null}
      <div className="flex flex-wrap items-center justify-end gap-3">
        {!empty ? (
          <div className="flex shrink-0 gap-2">
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button size="sm" variant="outline">
                  <Plus className="size-4" />
                  New
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onSelect={() => setCreating('deck')}>
                  <Layers />
                  Flashcard deck
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => setCreating('quiz')}>
                  <ListChecks />
                  Quiz
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
            <Button
              size="sm"
              onClick={startPractice}
              variant={recommended ? 'outline' : 'default'}
              disabled={createQuiz.isPending || !documentsLoaded || readyCount === 0}
            >
              {createQuiz.isPending ? <Spinner /> : null}
              New quiz
            </Button>
          </div>
        ) : null}
      </div>

      {/* The decks and quizzes above load from their own query; only Practice needs to
          know which documents are ready. So a failed document query dims exactly one
          thing, and says so rather than leaving the button dead without a word. */}
      {documents.isError ? (
        <p className="text-text-secondary flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
          <span>The document list did not load, so New quiz cannot tell what is ready.</span>
          <Button variant="outline" size="sm" onClick={() => void documents.refetch()}>
            Retry
          </Button>
        </p>
      ) : null}

      {documentsLoaded && readyCount === 0 ? (
        <p className="text-text-secondary text-sm">
          Add a document in Files and wait for it to be ready before creating a quiz.
        </p>
      ) : null}
      {!empty ? (
        <p className="text-text-tertiary text-xs">
          New quiz uses all ready class documents. Choose New to select sources.
        </p>
      ) : null}
      {decks.length + quizzes.length >= 10 || view.query ? (
        <div className="flex items-center gap-2">
          <Input
            type="search"
            aria-label="Search practice by title"
            placeholder="Search practice"
            value={view.query}
            onChange={(event) => updateView({ query: event.target.value, limit: 20 })}
          />
          {view.query ? (
            <Button variant="ghost" onClick={() => updateView({ query: '', limit: 20 })}>
              Clear search
            </Button>
          ) : null}
        </div>
      ) : null}
      {!empty && matches.length === 0 ? (
        <p role="status">No practice matches this search.</p>
      ) : null}
      {empty ? (
        <Empty className="py-12">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <Layers className="text-text-tertiary size-8" />
            </EmptyMedia>
            {/* "No decks or quizzes yet", not "nothing to study from": this state means
                no saved study artifacts exist, and in a class full of ready documents
                New quiz works perfectly well from here. */}
            <EmptyTitle>No decks or quizzes yet</EmptyTitle>
            <EmptyDescription>
              Create a quiz from all ready class documents, or choose your own sources.
            </EmptyDescription>
          </EmptyHeader>
          <div className="mt-2 flex flex-wrap justify-center gap-2">
            <Button
              onClick={startPractice}
              disabled={createQuiz.isPending || !documentsLoaded || readyCount === 0}
            >
              {createQuiz.isPending ? <Spinner /> : null}
              New quiz
            </Button>
            <Button variant="outline" onClick={() => setCreating('deck')}>
              New deck
            </Button>
            <Button variant="outline" onClick={() => setCreating('quiz')}>
              Choose quiz sources
            </Button>
          </div>
        </Empty>
      ) : (
        <>
          {visibleDecks.length > 0 ? (
            <StudySection title="Decks" icon={<Layers aria-hidden className="size-4" />}>
              {visibleDecks.map((deck) => (
                <li key={deck.id}>
                  <StudyRow
                    classId={classId}
                    artifact={deck}
                    description={describeDeck(deck)}
                    dueCount={deck.state === 'ready' ? deck.due_count : 0}
                    onRename={() => setRenaming({ kind: 'deck', id: deck.id, title: deck.title })}
                    onDelete={() => setDeleting({ kind: 'deck', id: deck.id, title: deck.title })}
                  />
                </li>
              ))}
            </StudySection>
          ) : null}

          {visibleQuizzes.length > 0 ? (
            <StudySection title="Quizzes" icon={<ListChecks aria-hidden className="size-4" />}>
              {visibleQuizzes.map((quiz) => (
                <li key={quiz.id}>
                  <StudyRow
                    classId={classId}
                    artifact={quiz}
                    description={describeQuiz(quiz)}
                    onRename={() => setRenaming({ kind: 'quiz', id: quiz.id, title: quiz.title })}
                    onDelete={() => setDeleting({ kind: 'quiz', id: quiz.id, title: quiz.title })}
                  />
                </li>
              ))}
            </StudySection>
          ) : null}
        </>
      )}

      {matches.length > view.limit ? (
        <Button variant="outline" onClick={() => updateView({ ...view, limit: view.limit + 20 })}>
          Show more practice ({matches.length - view.limit} remaining)
        </Button>
      ) : null}
      <CreateStudyDialog
        classId={classId}
        kind={creating}
        existingTitles={existingTitles}
        onOpenChange={(open) => {
          if (!open) setCreating(null)
        }}
      />

      <RenameDialog
        target={renaming ? { id: renaming.id, name: renaming.title } : null}
        title={renaming?.kind === 'quiz' ? 'Rename quiz' : 'Rename deck'}
        description="Choose a name that helps you find this study tool."
        label="Name"
        pending={renameDeck.isPending || renameQuiz.isPending}
        onOpenChange={(open) => {
          if (!open) setRenaming(null)
        }}
        onRename={(artifactId, title) =>
          renaming?.kind === 'quiz'
            ? renameQuiz.mutateAsync({ quizId: artifactId, title })
            : renameDeck.mutateAsync({ deckId: artifactId, title })
        }
      />

      <AlertDialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) setDeleting(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {deleting?.title}?</AlertDialogTitle>
            <AlertDialogDescription>
              {deleting?.kind === 'quiz'
                ? 'Every question in it goes, along with your past attempts. The documents it was built from stay. This cannot be undone.'
                : 'Every card in it goes, along with its scheduling history. The documents it was built from stay. This cannot be undone.'}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <Button
              variant="destructive"
              disabled={deleteDeck.isPending || deleteQuiz.isPending}
              onClick={() => void onConfirmDelete()}
            >
              {deleteDeck.isPending || deleteQuiz.isPending ? <Spinner /> : null}
              Delete
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

/** One kind of study tool, under the section heading the hub overview already uses. */
function StudySection({
  title,
  icon,
  children,
}: {
  title: string
  icon: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section className="flex flex-col gap-3" aria-label={title}>
      <div className="border-border/70 flex items-center gap-2 border-b pb-2">
        <span className="text-text-tertiary">{icon}</span>
        <h2 className="text-xs font-medium tracking-[0.14em] uppercase">{title}</h2>
      </div>
      <ul className="flex flex-col gap-2">{children}</ul>
    </section>
  )
}

type StudyRowProps = {
  classId: number
  artifact: StudyArtifactRead
  /** The second line: bucket counts for a ready deck, the stage line while generating. */
  description: string
  /** Shown as a badge when anything is due. */
  dueCount?: number
  onRename: () => void
  onDelete: () => void
}

/**
 * One deck or quiz in a list. The whole row is the link, and the actions menu sits above
 * it in the stacking order, so a row stays one target while Rename and Delete stay
 * independently clickable - the same arrangement as a solution row, for the same reason.
 */
function StudyRow({
  classId,
  artifact,
  description,
  dueCount = 0,
  onRename,
  onDelete,
}: StudyRowProps) {
  const failed = artifact.state === 'failed'

  return (
    <div className="group border-border bg-card hover:border-border-strong focus-within:border-border-strong relative flex items-center rounded-md border transition-colors">
      <Link
        href={`/classes/${classId}/study/${artifact.id}${dueCount > 0 ? '?review=due' : ''}`}
        className="focus-visible:ring-ring flex min-w-0 flex-1 flex-wrap items-center gap-2 rounded-md py-3 pr-1 pl-4 focus-visible:ring-2 focus-visible:outline-none"
      >
        <span className="flex min-w-0 basis-40 flex-1 flex-col gap-1">
          <span className="text-text-primary break-words font-medium">{artifact.title}</span>
          <span className="text-text-tertiary break-words text-xs">
            {description} · {formatRelativeTime(artifact.updated_at)}
          </span>
        </span>

        {dueCount > 0 ? <Badge variant="default">Review due · {dueCount}</Badge> : null}
        {failed ? (
          <span className="text-danger-text inline-flex shrink-0 items-center gap-1.5 text-xs">
            <FileWarning className="size-3.5" aria-hidden />
            {stateLabel(artifact.state)}
          </span>
        ) : artifact.state === 'ready' && artifact.kind === 'quiz' ? (
          <span className="text-text-secondary text-xs">
            {artifact.active_attempt_id != null ? 'Continue quiz' : 'Open quiz'}
          </span>
        ) : artifact.state !== 'ready' ? (
          <span className="text-text-tertiary shrink-0 text-xs">{stateLabel(artifact.state)}</span>
        ) : null}
      </Link>

      <div className="shrink-0 pr-2">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              aria-label={`Actions for ${artifact.title}`}
              className="size-8"
            >
              <MoreVertical />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={onRename}>
              <Pencil />
              Rename
            </DropdownMenuItem>
            <DropdownMenuItem variant="destructive" onSelect={onDelete}>
              <Trash2 />
              Delete
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  )
}

const QUIZ_TYPE_OPTIONS: { value: QuizQuestionType; label: string }[] = [
  { value: 'mcq', label: 'Multiple choice' },
  { value: 'true_false', label: 'True or false' },
  { value: 'fill_blank', label: 'Fill in the blank' },
]

const DIFFICULTY_OPTIONS: { value: QuizDifficulty; label: string }[] = [
  { value: 'basic', label: 'Basic' },
  { value: 'intermediate', label: 'Intermediate' },
  { value: 'exam', label: 'Exam' },
]

/** Keep raw input untouched; reject invalid values instead of changing the request. */
function validCount(raw: string, min: number, max: number): boolean {
  return /^\d+$/.test(raw) && Number(raw) >= min && Number(raw) <= max
}

/**
 * Asks Lyra to write a deck or a quiz from this class's documents.
 *
 * Both kinds share the two decisions that matter - what it is called and what it is built
 * from - so they share one dialog; the per-kind options hang off the same form. The
 * document list defaults to everything ready, which is what the backend does when the
 * list is left null.
 */
function CreateStudyDialog({
  classId,
  kind,
  existingTitles,
  onOpenChange,
}: {
  classId: number
  kind: 'deck' | 'quiz' | null
  /** Names already taken in this class, so the prefilled name never collides. */
  existingTitles: string[]
  onOpenChange: (open: boolean) => void
}) {
  const router = useRouter()
  const titleId = useId()
  const documents = useDocuments(classId, { enabled: kind !== null })
  const createDeck = useCreateDeck(classId)
  const createQuiz = useCreateQuiz(classId)

  const [title, setTitle] = useState('')
  /** Null means the student has not touched the list: every ready document. */
  const [selected, setSelected] = useState<number[] | null>(null)
  const [cardsPerTopic, setCardsPerTopic] = useState('4')
  const [count, setCount] = useState('10')
  const [difficulty, setDifficulty] = useState<QuizDifficulty>('intermediate')
  /** Null means every question type. */
  const [types, setTypes] = useState<QuizQuestionType[] | null>(null)
  const [optionsOpen, setOptionsOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Reset during render rather than in an effect, so reopening the dialog never shows the
  // previous attempt's form for a frame. The name arrives already filled in: a workable
  // default is one less decision standing between the student and the first question, and
  // the field is right there for anyone who wants a better one. Options collapses again
  // too: progressive disclosure is a promise about every opening, not just the first.
  const [kindSeen, setKindSeen] = useState(kind)
  if (kind !== kindSeen) {
    setKindSeen(kind)
    setTitle(kind === null ? '' : quickStudyTitle(kind, existingTitles))
    setSelected(null)
    setCardsPerTopic('4')
    setCount('10')
    setDifficulty('intermediate')
    setTypes(null)
    setOptionsOpen(false)
    setError(null)
  }

  const readyIds = (documents.data ?? [])
    .filter((document) => document.state === 'ready')
    .map((document) => document.id)
  const effectiveSelected = selected ?? readyIds
  const effectiveTypes = types ?? QUIZ_TYPE_OPTIONS.map((option) => option.value)
  const pending = createDeck.isPending || createQuiz.isPending

  function toggleDocument(documentId: number) {
    setSelected((current) => {
      const base = current ?? readyIds
      return base.includes(documentId)
        ? base.filter((id) => id !== documentId)
        : [...base, documentId]
    })
  }

  function toggleType(value: QuizQuestionType) {
    setTypes((current) => {
      const base = current ?? QUIZ_TYPE_OPTIONS.map((option) => option.value)
      return base.includes(value) ? base.filter((type) => type !== value) : [...base, value]
    })
  }

  const countValid = kind === 'deck' ? validCount(cardsPerTopic, 2, 6) : validCount(count, 3, 30)

  async function onSubmit() {
    if (!kind || !countValid || documents.isError || pending) return
    const trimmed = title.trim()
    if (!trimmed || effectiveSelected.length === 0) return
    try {
      const artifact =
        kind === 'deck'
          ? await createDeck.mutateAsync({
              title: trimmed,
              document_ids: effectiveSelected,
              cards_per_topic: Number(cardsPerTopic),
            })
          : await createQuiz.mutateAsync({
              title: trimmed,
              document_ids: effectiveSelected,
              count: Number(count),
              difficulty,
              types: effectiveTypes,
            })
      onOpenChange(false)
      router.push(`/classes/${classId}/study/${artifact.id}`)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not create this.')
    }
  }

  const canSubmit =
    title.trim().length > 0 &&
    effectiveSelected.length > 0 &&
    (kind !== 'quiz' || effectiveTypes.length > 0) &&
    countValid &&
    !documents.isError &&
    !pending

  return (
    <Dialog open={kind !== null} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{kind === 'quiz' ? 'New quiz' : 'New flashcard deck'}</DialogTitle>
          <DialogDescription>
            {kind === 'quiz'
              ? 'Lyra writes questions from the documents you pick, then grades your answers per topic.'
              : 'Lyra writes cards from the documents you pick, then schedules the reviews.'}
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-2">
          <Label htmlFor={titleId}>Name</Label>
          <Input
            id={titleId}
            value={title}
            autoFocus
            autoComplete="off"
            placeholder={kind === 'quiz' ? 'Week 4 quiz' : 'Week 4 flashcards'}
            onChange={(event) => setTitle(event.target.value)}
          />
        </div>

        <div className="grid gap-2">
          <Label>Source documents</Label>
          {documents.isError ? (
            <div className="text-danger-text text-sm" role="alert">
              <p>
                {documents.error instanceof ApiError
                  ? documents.error.message
                  : 'Could not load the documents.'}
              </p>
              <Button
                variant="outline"
                size="sm"
                className="mt-2"
                disabled={documents.isFetching}
                onClick={() => void documents.refetch()}
              >
                Retry documents
              </Button>
            </div>
          ) : (
            <SourcePicker
              name="study-sources"
              documents={documents.data ?? []}
              loading={documents.isPending}
              selected={effectiveSelected}
              onToggle={toggleDocument}
              emptyLabel="No documents in this class yet."
            />
          )}
          {!documents.isPending && readyIds.length === 0 && !documents.isError ? (
            <p className="text-text-tertiary text-sm">
              Nothing has finished processing yet, so there is nothing to study from.
            </p>
          ) : null}
        </div>

        {/* Every field below has a default the backend would have chosen anyway, so none
            of them stands between the student and Create. Closed, the dialog is name plus
            sources; open, the power user keeps every dial they had. */}
        <Collapsible open={optionsOpen} onOpenChange={setOptionsOpen}>
          <CollapsibleTrigger className="text-text-secondary hover:text-text-primary focus-visible:ring-ring flex items-center gap-1 rounded-sm text-sm transition-colors focus-visible:ring-2 focus-visible:outline-none">
            <ChevronRight
              className={cn('size-3.5 transition-transform', optionsOpen && 'rotate-90')}
              aria-hidden
            />
            Options
            <span className="text-text-tertiary text-xs">
              {!countValid
                ? 'Check the count'
                : kind === 'quiz'
                  ? `${count} questions · ${
                      DIFFICULTY_OPTIONS.find((option) => option.value === difficulty)?.label
                    }`
                  : `${cardsPerTopic} cards per topic`}
            </span>
          </CollapsibleTrigger>
          <CollapsibleContent className="flex flex-col gap-4 pt-3">
            {kind === 'deck' ? (
              <div className="grid gap-2">
                <Label htmlFor="cards-per-topic">Cards per topic</Label>
                <Input
                  id="cards-per-topic"
                  type="number"
                  min={2}
                  max={6}
                  value={cardsPerTopic}
                  onChange={(event) => setCardsPerTopic(event.target.value)}
                  step={1}
                  aria-invalid={!countValid}
                  aria-describedby="cards-per-topic-help"
                  className="w-24"
                />
                {!countValid ? (
                  <p id="cards-per-topic-help" role="alert" className="text-danger-text text-sm">
                    Enter a whole number from 2 to 6.
                  </p>
                ) : null}
              </div>
            ) : null}

            {kind === 'quiz' ? (
              <>
                <div className="grid gap-2">
                  <Label htmlFor="question-count">Questions</Label>
                  <Input
                    id="question-count"
                    type="number"
                    min={3}
                    max={30}
                    value={count}
                    onChange={(event) => setCount(event.target.value)}
                    step={1}
                    aria-invalid={!countValid}
                    aria-describedby="question-count-help"
                    className="w-24"
                  />
                  {!countValid ? (
                    <p id="question-count-help" role="alert" className="text-danger-text text-sm">
                      Enter a whole number from 3 to 30.
                    </p>
                  ) : null}
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="quiz-difficulty">Difficulty</Label>
                  <Select
                    value={difficulty}
                    onValueChange={(value) => setDifficulty(value as QuizDifficulty)}
                  >
                    <SelectTrigger id="quiz-difficulty" className="w-48">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {DIFFICULTY_OPTIONS.map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                {/* No multi-select primitive exists in the codebase, so the types are a
                    checkbox list, the same answer the document picker above gives. */}
                <fieldset className="grid gap-2">
                  <legend className="text-sm font-medium">Question types</legend>
                  {QUIZ_TYPE_OPTIONS.map((option) => (
                    <label key={option.value} className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        className="accent-accent-primary size-4"
                        checked={effectiveTypes.includes(option.value)}
                        onChange={() => toggleType(option.value)}
                      />
                      {option.label}
                    </label>
                  ))}
                </fieldset>
              </>
            ) : null}
          </CollapsibleContent>
        </Collapsible>

        {error ? (
          <p className="text-danger-text text-sm" role="alert">
            {error}
          </p>
        ) : null}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={!canSubmit} onClick={() => void onSubmit()}>
            {pending ? <Spinner /> : null}
            {kind === 'quiz' ? 'Create quiz' : 'Create deck'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
