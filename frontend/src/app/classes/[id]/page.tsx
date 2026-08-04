'use client'

import { useCallback, useState } from 'react'
import { FileText, SquareCheckBig } from 'lucide-react'
import Link from 'next/link'
import { useParams, useRouter, useSearchParams } from 'next/navigation'

import { ChatPane } from '@/components/chat/chat-pane'
import { DocumentsPane } from '@/components/documents/documents-pane'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useClass } from '@/lib/hooks/use-classes'
import { useDocuments } from '@/lib/hooks/use-documents'
import { useLocalStorageState } from '@/lib/hooks/use-local-storage-state'
import { useMediaQuery } from '@/lib/hooks/use-media-query'

function readClassId(value: string | string[] | undefined): number | null {
  const raw = Array.isArray(value) ? value[0] : value
  const classId = Number(raw)
  return Number.isSafeInteger(classId) && classId > 0 ? classId : null
}

function readSessionId(value: string | null): number | null {
  const sessionId = Number(value)
  return Number.isSafeInteger(sessionId) && sessionId > 0 ? sessionId : null
}

function parseOpen(raw: string): boolean {
  return raw === 'true'
}

export default function ClassWorkspacePage() {
  const params = useParams<{ id: string }>()
  const classId = readClassId(params.id)
  const compact = useMediaQuery('(max-width: 1023px)')
  const router = useRouter()
  const searchParams = useSearchParams()
  const [selectedDocumentId, setSelectedDocumentId] = useState<number | null>(null)

  // The conversation is part of the URL so sidebar chats are linkable and reloadable.
  // The URL is the only source of truth here: mirroring it into state meant every
  // navigation set state from an effect and re-rendered the workspace twice.
  const sessionId = readSessionId(searchParams.get('session'))

  const handleSessionIdChange = useCallback(
    (next: number | null) => {
      const target = next === null ? `/classes/${classId}` : `/classes/${classId}?session=${next}`
      router.replace(target, { scroll: false })
    },
    [classId, router],
  )

  // Documents open into the space the reading column was never going to use, so the
  // conversation does not shrink when the list appears. Closed by default, per class.
  const documentsStorageKey = `lyra-workspace-documents-open-${classId ?? 'unknown'}`
  const [documentsOpen, setDocumentsOpen] = useLocalStorageState(
    documentsStorageKey,
    false,
    parseOpen,
  )

  const classQuery = useClass(classId ?? Number.NaN)
  const { data: documentList } = useDocuments(classId ?? Number.NaN)
  const documentCount = documentList?.length ?? null

  if (classId === null) {
    return (
      <Alert variant="destructive" className="max-w-xl">
        <AlertTitle>That class could not be opened</AlertTitle>
        <AlertDescription>Return to Classes and choose a class from the list.</AlertDescription>
      </Alert>
    )
  }

  if (classQuery.isError) {
    return (
      <Alert variant="destructive" className="max-w-xl">
        <AlertTitle>Could not load this class</AlertTitle>
        <AlertDescription>
          <p>
            {classQuery.error instanceof Error ? classQuery.error.message : 'Try loading it again.'}
          </p>
          <Button
            variant="outline"
            size="sm"
            className="mt-3"
            onClick={() => void classQuery.refetch()}
          >
            Retry
          </Button>
        </AlertDescription>
      </Alert>
    )
  }

  const className = classQuery.data?.name ?? 'Class'
  const documents = (
    <DocumentsPane
      classId={classId}
      selectedDocumentId={selectedDocumentId}
      onSelectDocument={setSelectedDocumentId}
      onClose={compact ? undefined : () => setDocumentsOpen(false)}
    />
  )
  const chat = (
    <ChatPane
      key={classId}
      classId={classId}
      className={className}
      selectedDocumentId={selectedDocumentId}
      onClearSelectedDocument={() => setSelectedDocumentId(null)}
      sessionId={sessionId}
      onSessionIdChange={handleSessionIdChange}
      headerActions={
        <>
          {/* Solve is the third rung of the Guide/Show/Solve ladder, and until this
              existed it was the only rung with no way into it from the workspace: the
              solver lived exclusively in a sidebar sub-item below the conversation list.
              A peer of Documents rather than of Guide and Show, because those two change
              how the current answer is written and this opens something else. */}
          <Button variant="ghost" size="sm" className="h-8" asChild>
            <Link href={`/classes/${classId}/solutions`}>
              <SquareCheckBig aria-hidden className="size-3.5" />
              Solve
            </Link>
          </Button>
          {compact ? null : (
            <Button
              variant="ghost"
              size="sm"
              className="h-8"
              aria-expanded={documentsOpen}
              aria-controls="documents-pane-body"
              onClick={() => setDocumentsOpen(!documentsOpen)}
            >
              <FileText aria-hidden className="size-3.5" />
              Documents
              {documentCount === null ? null : (
                <span className="text-text-tertiary tabular-nums">{documentCount}</span>
              )}
            </Button>
          )}
        </>
      }
    />
  )

  // One raised-paper workbench, not two floating cards: the conversation and its
  // documents are one workspace and read as one surface.
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {compact ? (
        <Tabs
          defaultValue="chat"
          className="min-h-0 flex-1 gap-0 overflow-hidden rounded-lg border bg-card shadow-sm"
        >
          <TabsList variant="line" aria-label="Workspace panes" className="px-4">
            <TabsTrigger value="chat">Chat</TabsTrigger>
            <TabsTrigger value="documents">
              Documents
              {documentCount === null ? null : (
                <span className="text-text-tertiary tabular-nums">{documentCount}</span>
              )}
            </TabsTrigger>
          </TabsList>
          <TabsContent
            value="chat"
            className="mt-0 min-h-0 flex-1 overflow-hidden rounded-none border-0"
          >
            {chat}
          </TabsContent>
          <TabsContent
            value="documents"
            className="mt-0 min-h-0 flex-1 overflow-hidden rounded-none border-0"
          >
            {documents}
          </TabsContent>
        </Tabs>
      ) : (
        <div className="flex min-h-0 flex-1 overflow-hidden rounded-lg border bg-card shadow-sm">
          <div className="min-w-0 flex-1">{chat}</div>
          {documentsOpen ? (
            <div className="w-[340px] shrink-0 border-l xl:w-[380px]">{documents}</div>
          ) : null}
        </div>
      )}
    </div>
  )
}
