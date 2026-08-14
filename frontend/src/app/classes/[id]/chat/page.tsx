'use client'

import { useCallback, useEffect, useState } from 'react'
import { Bot, FileText, SquareCheckBig } from 'lucide-react'
import Link from 'next/link'
import { useParams, useRouter, useSearchParams } from 'next/navigation'

import { ChatPane } from '@/components/chat/chat-pane'
import { AgentPanel } from '@/components/agent/agent-panel'
import { DocumentsPane } from '@/components/documents/documents-pane'
import { useFullBleed } from '@/components/layout/page-chrome'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { CHAT_HANDOFF_PARAMS } from '@/lib/handoff'
import { useClass } from '@/lib/hooks/use-classes'
import { useDocuments } from '@/lib/hooks/use-documents'
import { useLocalStorageState } from '@/lib/hooks/use-local-storage-state'
import { useMediaQuery } from '@/lib/hooks/use-media-query'

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

function parseOpen(raw: string): boolean {
  return raw === 'true'
}

export default function ClassWorkspacePage() {
  const params = useParams<{ id: string }>()
  const classId = readClassId(params.id)
  const compact = useMediaQuery('(max-width: 1023px)')
  const router = useRouter()
  const searchParams = useSearchParams()

  // A handoff from another surface: a question carried in from a quiz miss or the class
  // landing, or a document to scope the first question to. Captured once, on the render
  // the page arrives on, then stripped from the URL below so Back and reload never carry
  // the question in again.
  const [handoff] = useState(() => ({
    ask: searchParams.get('ask'),
    send: searchParams.get('send') === '1',
    documentId: readSessionId(searchParams.get('document')),
  }))

  const [selectedDocumentId, setSelectedDocumentId] = useState<number | null>(handoff.documentId)

  useEffect(() => {
    if (!CHAT_HANDOFF_PARAMS.some((param) => searchParams.has(param))) return
    const next = new URLSearchParams(searchParams)
    for (const param of CHAT_HANDOFF_PARAMS) next.delete(param)
    const query = next.toString()
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

  // Documents open into the space the reading column was never going to use, so the
  // conversation does not shrink when the list appears. Closed by default, per class.
  const documentsStorageKey = `lyra-workspace-documents-open-${classId ?? 'unknown'}`
  const [documentsOpen, setDocumentsOpen] = useLocalStorageState(
    documentsStorageKey,
    false,
    parseOpen,
  )
  const agentStorageKey = `lyra-workspace-agent-open-${classId ?? 'unknown'}`
  const [agentOpen, setAgentOpen] = useLocalStorageState(agentStorageKey, false, parseOpen)

  const classQuery = useClass(classId ?? Number.NaN)
  const { data: documentList } = useDocuments(classId ?? Number.NaN)
  const documentCount = documentList?.length ?? null

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
      draft={draftSession}
      initialAsk={handoff.ask}
      initialSend={handoff.send}
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
            <>
              <Button
                variant="ghost"
                size="sm"
                className="h-8"
                aria-expanded={agentOpen}
                aria-controls="agent-pane-body"
                onClick={() => {
                  setAgentOpen(!agentOpen)
                  if (!agentOpen) setDocumentsOpen(false)
                }}
              >
                <Bot aria-hidden className="size-3.5" />
                Agent
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="h-8"
                aria-expanded={documentsOpen}
                aria-controls="documents-pane-body"
                onClick={() => {
                  setDocumentsOpen(!documentsOpen)
                  if (!documentsOpen) setAgentOpen(false)
                }}
              >
                <FileText aria-hidden className="size-3.5" />
                Documents
                {documentCount === null ? null : (
                  <span className="text-text-tertiary tabular-nums">{documentCount}</span>
                )}
              </Button>
            </>
          )}
        </>
      }
    />
  )

  // One workbench filling the window, not a card floating in the middle of it. The
  // conversation and its documents are the page here, so they get the page.
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {compact ? (
        <Tabs defaultValue="chat" className="min-h-0 flex-1 gap-0 overflow-hidden bg-background">
          <TabsList variant="line" aria-label="Workspace panes" className="px-4">
            <TabsTrigger value="chat">Chat</TabsTrigger>
            <TabsTrigger value="documents">
              Documents
              {documentCount === null ? null : (
                <span className="text-text-tertiary tabular-nums">{documentCount}</span>
              )}
            </TabsTrigger>
            <TabsTrigger value="agent">Agent</TabsTrigger>
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
          <TabsContent
            value="agent"
            className="mt-0 min-h-0 flex-1 overflow-hidden rounded-none border-0"
          >
            <AgentPanel classId={classId} sessionId={sessionId} />
          </TabsContent>
        </Tabs>
      ) : (
        <div className="flex min-h-0 flex-1 overflow-hidden bg-background">
          <div className="min-w-0 flex-1">{chat}</div>
          {documentsOpen ? (
            <div className="w-[340px] shrink-0 border-l xl:w-[380px]">{documents}</div>
          ) : null}
          {agentOpen ? (
            <div id="agent-pane-body" className="w-[420px] shrink-0 border-l">
              <AgentPanel
                classId={classId}
                sessionId={sessionId}
                onClose={() => setAgentOpen(false)}
              />
            </div>
          ) : null}
        </div>
      )}
    </div>
  )
}
