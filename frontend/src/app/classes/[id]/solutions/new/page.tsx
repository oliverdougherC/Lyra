'use client'

import { FileText } from 'lucide-react'
import { useParams, useRouter } from 'next/navigation'
import { useMemo, useState, type ReactNode } from 'react'
import { toast } from 'sonner'

import { SourcePicker } from '@/components/solutions/source-picker'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ApiError } from '@/lib/api'
import { formatCount } from '@/lib/format'
import { useClass } from '@/lib/hooks/use-classes'
import { useDocuments } from '@/lib/hooks/use-documents'
import { useCreateSolution } from '@/lib/hooks/use-solutions'

/**
 * A raised-paper section, matching the Settings screen.
 *
 * The vendored `Card` leaves `--card-spacing` unset at its default size, so `CardHeader`
 * and `CardContent` resolve their padding to nothing and the title sits on the border.
 * Settings already works around this by supplying its own padding, and this follows that
 * rather than editing a primitive in `components/ui/`.
 */
function SetupSection({
  title,
  description,
  children,
}: {
  title: string
  description: string
  children: ReactNode
}) {
  return (
    <Card className="gap-4 py-0">
      <div className="border-b px-5 pt-5 pb-4">
        <h2 className="font-heading text-xl leading-tight font-medium tracking-tight">{title}</h2>
        <p className="text-text-secondary mt-1 text-sm">{description}</p>
      </div>
      <div className="px-5 pb-5">{children}</div>
    </Card>
  )
}

function readClassId(value: string | string[] | undefined): number | null {
  const raw = Array.isArray(value) ? value[0] : value
  const classId = Number(raw)
  return Number.isSafeInteger(classId) && classId > 0 ? classId : null
}

export default function NewSolutionPage() {
  const params = useParams<{ id: string }>()
  const classId = readClassId(params.id)
  const router = useRouter()

  const [problemSet, setProblemSet] = useState<number[]>([])
  const [reference, setReference] = useState<number[]>([])
  const [title, setTitle] = useState('')
  const [titleTouched, setTitleTouched] = useState(false)

  const classQuery = useClass(classId ?? Number.NaN)
  // Ingestion is polled here too: a file dropped on the workspace a moment ago should
  // become selectable on this screen without a reload.
  const documentsQuery = useDocuments(classId ?? Number.NaN, { refetchInterval: 2000 })
  const create = useCreateSolution(classId ?? Number.NaN)

  const documents = useMemo(() => documentsQuery.data ?? [], [documentsQuery.data])
  const suggestedTitle = useMemo(() => {
    const first = documents.find((document) => document.id === problemSet[0])
    return first ? first.filename.replace(/\.[^.]+$/, '') : ''
  }, [documents, problemSet])

  const effectiveTitle = titleTouched ? title : suggestedTitle

  if (classId === null) {
    return (
      <Alert variant="destructive">
        <AlertTitle>That class link is not valid</AlertTitle>
        <AlertDescription>Go back to your classes and open one from there.</AlertDescription>
      </Alert>
    )
  }

  const toggle = (list: number[], setter: (next: number[]) => void) => (documentId: number) =>
    setter(
      list.includes(documentId) ? list.filter((id) => id !== documentId) : [...list, documentId],
    )

  const handleSubmit = () => {
    create.mutate(
      {
        sources: [
          ...problemSet.map((documentId) => ({
            document_id: documentId,
            role: 'problem_set' as const,
          })),
          ...reference.map((documentId) => ({
            document_id: documentId,
            role: 'reference_solutions' as const,
          })),
        ],
        title: effectiveTitle.trim() || null,
      },
      {
        onSuccess: (solution) => router.push(`/classes/${classId}/solutions/${solution.id}`),
        onError: (error) =>
          toast.error(
            error instanceof ApiError ? error.message : 'Could not start this solution set.',
          ),
      },
    )
  }

  const nothingToSolve = !documentsQuery.isPending && documents.length === 0

  return (
    <div className="mx-auto flex w-full max-w-[720px] flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h1 className="font-heading text-text-primary text-2xl tracking-tight">New solution set</h1>
        <p className="text-text-secondary text-sm">
          {classQuery.data
            ? `Lyra will read a problem set from ${classQuery.data.name} and list its problems for you to check.`
            : 'Lyra will read a problem set and list its problems for you to check.'}
        </p>
      </header>

      {documentsQuery.isError ? (
        <Alert variant="destructive">
          <AlertTitle>Could not load your documents</AlertTitle>
          <AlertDescription>
            {documentsQuery.error instanceof ApiError
              ? documentsQuery.error.message
              : 'Something went wrong.'}
          </AlertDescription>
        </Alert>
      ) : null}

      {nothingToSolve ? (
        <Empty className="py-12">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <FileText className="text-text-tertiary size-8" />
            </EmptyMedia>
            <EmptyTitle>Nothing to solve yet</EmptyTitle>
            <EmptyDescription>
              Upload a problem set to this class and Lyra will read it first. You can do that from
              the class workspace.
            </EmptyDescription>
          </EmptyHeader>
          <Button asChild className="mt-4">
            <a href={`/classes/${classId}`}>Go to the workspace</a>
          </Button>
        </Empty>
      ) : (
        <>
          <SetupSection
            title="Problem set"
            description="The homework to solve. Pick more than one file if the set spans several."
          >
            <SourcePicker
              name="problem-set"
              documents={documents}
              loading={documentsQuery.isPending}
              selected={problemSet}
              claimed={reference}
              onToggle={toggle(problemSet, setProblemSet)}
              emptyLabel="No documents in this class yet."
            />
          </SetupSection>

          <SetupSection
            title="Reference solutions"
            description="Optional. If you have solutions from an earlier set, Lyra will follow their notation and method."
          >
            <SourcePicker
              name="reference-solutions"
              documents={documents}
              loading={documentsQuery.isPending}
              selected={reference}
              claimed={problemSet}
              onToggle={toggle(reference, setReference)}
              emptyLabel="No documents in this class yet."
            />
          </SetupSection>

          <SetupSection title="Name" description="What this set is called in your sidebar.">
            <Label htmlFor="solution-title" className="sr-only">
              Name
            </Label>
            <Input
              id="solution-title"
              value={effectiveTitle}
              placeholder="Problem set 4"
              onChange={(event) => {
                setTitleTouched(true)
                setTitle(event.target.value)
              }}
            />
          </SetupSection>

          <div className="flex flex-wrap items-center gap-3">
            {/* The primary action says what pressing it does. Solving does not start here:
                it waits for the problem list to be checked. */}
            <Button
              size="lg"
              onClick={handleSubmit}
              disabled={problemSet.length === 0 || create.isPending}
            >
              {create.isPending ? 'Starting' : 'Find problems'}
            </Button>
            {problemSet.length === 0 ? (
              <p className="text-text-tertiary text-sm">Pick at least one problem set first.</p>
            ) : (
              <p className="text-text-tertiary text-sm">
                {formatCount(problemSet.length, 'file')} to read
                {reference.length > 0 ? `, ${formatCount(reference.length, 'reference file')}` : ''}
              </p>
            )}
          </div>
        </>
      )}
    </div>
  )
}
