'use client'

import { useCallback, useEffect, useState } from 'react'
import { Bot } from 'lucide-react'
import { useParams, useRouter, useSearchParams } from '@/router/hooks'

import { AgentPanel } from '@/components/agent/agent-panel'
import { ChatPane } from '@/components/chat/chat-pane'
import { SourceContext } from '@/components/chat/source-context'
import { useFullBleed } from '@/components/layout/page-chrome'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { readChatHandoff, stripChatHandoff } from '@/lib/handoff'
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

/**
 * The class conversation, alone in the window.
 *
 * The workspace is the page here: the whole window is the workbench, and the only
 * question it owns is which material the answer reads. The documents column moved out to
 * the class Files tab. The agent column stays as a temporary compatibility bridge (see
 * below): its replacement is the contextual agent in #65, and until that is merged the
 * existing entry point must keep working.
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

  const [selectedDocumentId, setSelectedDocumentId] = useState<number | null>(handoff.documentId)

  useEffect(() => {
    const stripped = stripChatHandoff(searchParams)
    if (stripped === null) return
    const query = stripped.toString()
    router.replace(`/classes/${classId}/chat${query ? `?${query}` : ''}`, { scroll: false })
  }, [classId, router, searchParams])

  // ── Temporary Agent compatibility bridge (removed by #65) ─────────────────────────
  // This page used to be the agent's only door: a header toggle opening the AgentPanel
  // column, a workspace tab below 1024px. #64 removes the surrounding chrome (documents
  // column, Solve button) but not this door, because #65's contextual agent does not exist
  // yet and main must never lose a working capability. It is kept deliberately un-
  // prominent - the same ghost button and column it was - and #65 deletes it when the
  // replacement entry point lands.
  const compact = useMediaQuery('(max-width: 1023px)')
  const agentStorageKey = `lyra-workspace-agent-open-${classId ?? 'unknown'}`
  const [agentOpen, setAgentOpen] = useLocalStorageState(agentStorageKey, false, parseOpen)

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
      headerActions={
        // The bridge's door on the desktop layout: the toggle the pre-#64 workspace had.
        compact ? null : (
          <Button
            variant="ghost"
            size="sm"
            className="h-8"
            aria-expanded={agentOpen}
            aria-controls="agent-pane-body"
            onClick={() => setAgentOpen(!agentOpen)}
          >
            <Bot aria-hidden className="size-3.5" />
            Agent
          </Button>
        )
      }
    />
  )

  if (compact) {
    // Below the rail line the agent is a pane of the workspace, exactly as it was before
    // this pass: Chat and Agent share the window, one at a time.
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <Tabs defaultValue="chat" className="min-h-0 flex-1 gap-0 overflow-hidden bg-background">
          <TabsList variant="line" aria-label="Workspace panes" className="px-4">
            <TabsTrigger value="chat">Chat</TabsTrigger>
            <TabsTrigger value="agent">Agent</TabsTrigger>
          </TabsList>
          <TabsContent
            value="chat"
            className="mt-0 min-h-0 flex-1 overflow-hidden rounded-none border-0"
          >
            {chat}
          </TabsContent>
          <TabsContent
            value="agent"
            className="mt-0 min-h-0 flex-1 overflow-hidden rounded-none border-0"
          >
            <AgentPanel classId={classId} sessionId={sessionId} />
          </TabsContent>
        </Tabs>
      </div>
    )
  }

  // The conversation is the page; the agent column is the bridge's other half, docked to
  // its right, as before this pass.
  return (
    <div className="flex min-h-0 flex-1 overflow-hidden bg-background">
      <div className="min-w-0 flex-1">{chat}</div>
      {agentOpen ? (
        <div id="agent-pane-body" className="w-[420px] shrink-0 border-l">
          <AgentPanel classId={classId} sessionId={sessionId} onClose={() => setAgentOpen(false)} />
        </div>
      ) : null}
    </div>
  )
}
