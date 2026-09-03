'use client'

import { useCallback, useEffect, useState } from 'react'
import { useParams, useRouter, useSearchParams } from '@/router/hooks'

import { ChatPane } from '@/components/chat/chat-pane'
import { SourceContext } from '@/components/chat/source-context'
import { useFullBleed } from '@/components/layout/page-chrome'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { readChatHandoff, stripChatHandoff } from '@/lib/handoff'
import { useClass } from '@/lib/hooks/use-classes'
import { useDocuments } from '@/lib/hooks/use-documents'

function readClassId(value: string | string[] | undefined): number | null {
  const raw = Array.isArray(value) ? value[0] : value
  const classId = Number(raw)
  return Number.isSafeInteger(classId) && classId > 0 ? classId : null
}

/** The URL value meaning "a conversation that has not been started yet". */
const DRAFT_SESSION = 'new'

function readSessionId(value: string | null): number | null {
  const sessionId = Number(value)
  return Number.isSafeInteger(sessionId) && sessionId > 0 ? sessionId : null
}

/**
 * The class conversation, alone in the window.
 *
 * The workspace is the page here: the whole window is the workbench, and the only
 * question it owns is which material the answer reads. The documents column and the
 * agent column were two permanent destinations that moved out - documents to the class
 * Files tab, the agent to the class itself (C). What stays is the conversation, the
 * Guide/Show toggle, and the composer with its source context.
 */
export default function ClassWorkspacePage() {
  const params = useParams<{ id: string }>()
  const classId = readClassId(params.id)
  const router = useRouter()
  const searchParams = useSearchParams()

  // A handoff from another surface: a question carried in from a quiz miss or the class
  // landing, or a document to scope the first question to. Captured once, on the render
  // the page arrives on, then stripped from the URL below so Back and reload never carry
  // the question in again. Capturing once is safe only because every link that carries
  // these params comes from another route (see readChatHandoff): a same-route link would
  // change the params under a mounted page and never be applied.
  const [handoff] = useState(() => readChatHandoff(searchParams))

  const [selectedDocumentId, setSelectedDocumentId] = useState<number | null>(
    handoff.documentId,
  )

  useEffect(() => {
    const stripped = stripChatHandoff(searchParams)
    if (stripped === null) return
    const query = stripped.toString()
    router.replace(`/classes/${classId}/chat${query ? `?${query}` : ''}`, { scroll: false })
  }, [classId, router, searchParams])

  // The conversation is part of the URL so sidebar chats are linkable and reloadable.
  // The URL is the only source of truth here: mirroring it into state meant every
  // navigation set state from an effect and re-rendered the workspace twice.
  const sessionParam = searchParams.get('session')
  const sessionId = readSessionId(sessionParam)
  // `?session=new` is a conversation the student has opened but not yet started. It has no
  // row on the server until they send something, which is what keeps unused chats out of
  // the rail entirely rather than cleaning them up afterwards.
  const draftSession = sessionParam === DRAFT_SESSION

  const handleSessionIdChange = useCallback(
    (next: number | null) => {
      const base = `/classes/${classId}/chat`
      router.replace(next === null ? base : `${base}?session=${next}`, { scroll: false })
    },
    [classId, router],
  )

  const classQuery = useClass(classId ?? Number.NaN)
  const documentsQuery = useDocuments(classId ?? Number.NaN)

  // The workspace is the page here, so it gets the window. Not while the class failed to
  // load: that is an alert in a column, which wants the ordinary frame.
  useFullBleed(classId !== null && !classQuery.isError)

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

  // The composer's source context: one chip, one click into a popover, one answer to
  // "what Lyra reads for this question". Everything ready is the default; a single file
  // is a deliberate scope. Managing those files is not this page's job - the class Files
  // tab owns that.
  const sourceControl = (
    <SourceContext
      documents={documentsQuery.data}
      documentsError={documentsQuery.error}
      onRetryDocuments={() => void documentsQuery.refetch()}
      selectedId={selectedDocumentId}
      onSelect={setSelectedDocumentId}
    />
  )

  // The conversation is part of the URL so sidebar chats are linkable and reloadable.
  const chat = (
    <ChatPane
      key={classId}
      classId={classId}
      className={className}
      selectedDocumentId={selectedDocumentId}
      sessionId={sessionId}
      draft={draftSession}
      initialAsk={handoff.ask}
      initialSend={handoff.send}
      sourceControl={sourceControl}
      onSessionIdChange={handleSessionIdChange}
    />
  )

  return <>{chat}</>
}
