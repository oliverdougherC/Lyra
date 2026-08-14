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
import Link from 'next/link'
import { useRouter } from 'next/navigation'
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
  const buckets = deck.buckets
  const counts = `new ${buckets.new} · learning ${buckets.learning} · mastered ${buckets.mastered}`
  return `${formatCount(deck.cards_total, 'card')} · ${counts}`
}

function describeQuiz(quiz: StudyArtifactRead): string {
  if (quiz.state === 'pending' || quiz.state === 'generating') {
    return quiz.stage_detail ?? stateLabel(quiz.state)
  }
  return formatCount(quiz.problems_total ?? 0, 'question')
}

/** A deck or quiz being renamed or deleted; the kind picks the mutation. */
type StudyTarget = { kind: 'deck' | 'quiz'; id: number; title: string }

/** Every deck and quiz in a class, with the actions the solutions panel taught. */
export function ClassStudyPanel({ classId }: { classId: number }) {
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

  const readyCount = (documents.data ?? []).filter((item) => item.state === 'ready').length

  /**
   * The one-click way in: a quiz from everything ready, named after the day, at the
   * defaults the dialog would have offered anyway. The dialog remains for anyone who
   * wants to choose sources, counts, or difficulty; nobody has to visit it to start
   * studying.
   */
  function startPractice() {
    if (readyCount === 0) {
      toast.error('Nothing has finished processing yet, so there is nothing to practice from.')
      return
    }
    createQuiz.mutate(
      { title: quickStudyTitle('quiz') },
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

  if (study.isError) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load your study tools</AlertTitle>
        <AlertDescription>
          <p>{study.error instanceof ApiError ? study.error.message : 'Something went wrong.'}</p>
          <Button variant="outline" size="sm" className="mt-3" onClick={() => void study.refetch()}>
            Retry
          </Button>
        </AlertDescription>
      </Alert>
    )
  }

  const { decks, quizzes } = study.data
  const empty = decks.length === 0 && quizzes.length === 0

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between gap-3">
        <p className="text-text-secondary text-sm">
          Flashcard decks and quizzes Lyra writes from this class&apos;s documents.
        </p>
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
            <Button size="sm" onClick={startPractice} disabled={createQuiz.isPending}>
              {createQuiz.isPending ? <Spinner /> : null}
              Practice now
            </Button>
          </div>
        ) : null}
      </div>

      {empty ? (
        <Empty className="py-12">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <Layers className="text-text-tertiary size-8" />
            </EmptyMedia>
            <EmptyTitle>Nothing to study from yet</EmptyTitle>
            <EmptyDescription>
              Practice now writes a quiz from everything Lyra has read for this class. Or build a
              deck or quiz your own way, choosing the sources and difficulty yourself.
            </EmptyDescription>
          </EmptyHeader>
          <div className="mt-2 flex flex-wrap justify-center gap-2">
            <Button onClick={startPractice} disabled={createQuiz.isPending}>
              {createQuiz.isPending ? <Spinner /> : null}
              Practice now
            </Button>
            <Button variant="outline" onClick={() => setCreating('deck')}>
              New deck
            </Button>
            <Button variant="outline" onClick={() => setCreating('quiz')}>
              New quiz
            </Button>
          </div>
        </Empty>
      ) : (
        <>
          {decks.length > 0 ? (
            <StudySection title="Decks" icon={<Layers aria-hidden className="size-4" />}>
              {decks.map((deck) => (
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

          {quizzes.length > 0 ? (
            <StudySection title="Quizzes" icon={<ListChecks aria-hidden className="size-4" />}>
              {quizzes.map((quiz) => (
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

      <CreateStudyDialog
        classId={classId}
        kind={creating}
        onOpenChange={(open) => {
          if (!open) setCreating(null)
        }}
      />

      <RenameDialog
        target={renaming ? { id: renaming.id, name: renaming.title } : null}
        title={renaming?.kind === 'quiz' ? 'Rename quiz' : 'Rename deck'}
        description="It was named in a hurry, which is rarely what the work is."
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
        href={`/classes/${classId}/study/${artifact.id}`}
        className="focus-visible:ring-ring flex min-w-0 flex-1 items-center gap-4 rounded-md py-3 pr-1 pl-4 focus-visible:ring-2 focus-visible:outline-none"
      >
        <span className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="text-text-primary truncate font-medium">{artifact.title}</span>
          <span className="text-text-tertiary truncate text-xs">
            {description} · {formatRelativeTime(artifact.updated_at)}
          </span>
        </span>

        {dueCount > 0 ? <Badge variant="default">{dueCount} due</Badge> : null}
        {failed ? (
          <span className="text-danger-text inline-flex shrink-0 items-center gap-1.5 text-xs">
            <FileWarning className="size-3.5" aria-hidden />
            {stateLabel(artifact.state)}
          </span>
        ) : (
          <span className="text-text-tertiary shrink-0 text-xs">{stateLabel(artifact.state)}</span>
        )}
      </Link>

      <div className="shrink-0 pr-2">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              aria-label={`Actions for ${artifact.title}`}
              className="size-8 opacity-0 transition-opacity group-focus-within:opacity-100 group-hover:opacity-100 focus-visible:opacity-100 data-[state=open]:opacity-100"
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

/** Whole-number input state is text while typing; clamped to the API's range on submit. */
function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
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
  onOpenChange,
}: {
  classId: number
  kind: 'deck' | 'quiz' | null
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
  const [cardsPerTopic, setCardsPerTopic] = useState(4)
  const [count, setCount] = useState(10)
  const [difficulty, setDifficulty] = useState<QuizDifficulty>('intermediate')
  /** Null means every question type. */
  const [types, setTypes] = useState<QuizQuestionType[] | null>(null)
  const [optionsOpen, setOptionsOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Reset during render rather than in an effect, so reopening the dialog never shows the
  // previous attempt's form for a frame. The name arrives already filled in: a workable
  // default is one less decision standing between the student and the first question, and
  // the field is right there for anyone who wants a better one.
  const [kindSeen, setKindSeen] = useState(kind)
  if (kind !== kindSeen) {
    setKindSeen(kind)
    setTitle(kind === null ? '' : quickStudyTitle(kind))
    setSelected(null)
    setCardsPerTopic(4)
    setCount(10)
    setDifficulty('intermediate')
    setTypes(null)
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

  async function onSubmit() {
    if (!kind) return
    const trimmed = title.trim()
    if (!trimmed || effectiveSelected.length === 0) return
    try {
      const artifact =
        kind === 'deck'
          ? await createDeck.mutateAsync({
              title: trimmed,
              document_ids: effectiveSelected,
              cards_per_topic: clamp(cardsPerTopic, 2, 6),
            })
          : await createQuiz.mutateAsync({
              title: trimmed,
              document_ids: effectiveSelected,
              count: clamp(count, 3, 30),
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
            <p className="text-danger-text text-sm" role="alert">
              {documents.error instanceof ApiError
                ? documents.error.message
                : 'Could not load the documents.'}
            </p>
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
              {kind === 'quiz'
                ? `${clamp(count, 3, 30)} questions · ${
                    DIFFICULTY_OPTIONS.find((option) => option.value === difficulty)?.label
                  }`
                : `${clamp(cardsPerTopic, 2, 6)} cards per topic`}
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
                  onChange={(event) => setCardsPerTopic(Number(event.target.value))}
                  className="w-24"
                />
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
                    onChange={(event) => setCount(Number(event.target.value))}
                    className="w-24"
                  />
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
