'use client'

import { useCallback, useMemo, useRef, useState } from 'react'
import { FileText, UserRound } from 'lucide-react'
import { useParams } from 'next/navigation'

import { ChatPane } from '@/components/chat/chat-pane'
import { DocumentsPane } from '@/components/documents/documents-pane'
import { ClassProfileSheet } from '@/components/profile/class-profile-sheet'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from '@/components/ui/resizable'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useClass } from '@/lib/hooks/use-classes'
import { useLocalStorageState } from '@/lib/hooks/use-local-storage-state'
import { useMediaQuery } from '@/lib/hooks/use-media-query'

function parseLayout(raw: string): string | null {
  const [left, right] = raw.split(',').map(Number)
  return Number.isFinite(left) && Number.isFinite(right) && left > 10 && right > 10 ? raw : null
}

function readClassId(value: string | string[] | undefined): number | null {
  const raw = Array.isArray(value) ? value[0] : value
  const classId = Number(raw)
  return Number.isSafeInteger(classId) && classId > 0 ? classId : null
}

export default function ClassWorkspacePage() {
  const params = useParams<{ id: string }>()
  const classId = readClassId(params.id)
  const compact = useMediaQuery('(max-width: 1023px)')
  const [profileOpen, setProfileOpen] = useState(false)
  const profileTriggerRef = useRef<HTMLButtonElement>(null)
  const onProfileOpenChange = useCallback((open: boolean) => {
    setProfileOpen(open)
    if (!open) requestAnimationFrame(() => profileTriggerRef.current?.focus())
  }, [])
  const [selectedDocumentId, setSelectedDocumentId] = useState<number | null>(null)
  const layoutKey = `lyra-workspace-layout-${classId ?? 'unknown'}`
  const [storedLayout, setStoredLayout] = useLocalStorageState(layoutKey, '40,60', parseLayout)
  const panelLayout = useMemo(() => {
    const [documents, chat] = storedLayout.split(',').map(Number)
    return { documents, chat }
  }, [storedLayout])
  const classQuery = useClass(classId ?? Number.NaN)

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
    />
  )
  const chat = (
    <ChatPane
      key={classId}
      classId={classId}
      className={className}
      selectedDocumentId={selectedDocumentId}
      onClearSelectedDocument={() => setSelectedDocumentId(null)}
    />
  )

  return (
    <div className="flex h-[calc(100dvh-8.5rem)] flex-col gap-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          {classQuery.data?.code ? (
            <p className="text-text-tertiary text-xs font-medium tracking-[0.16em] uppercase">
              {classQuery.data.code}
            </p>
          ) : null}
          <h1 className="text-3xl leading-tight font-medium md:text-4xl">{className}</h1>
        </div>
        <Button ref={profileTriggerRef} variant="outline" onClick={() => setProfileOpen(true)}>
          <UserRound aria-hidden className="size-4" />
          Profile
        </Button>
      </div>

      <ClassProfileSheet classId={classId} open={profileOpen} onOpenChange={onProfileOpenChange} />

      {compact ? (
        <Tabs
          defaultValue="documents"
          className="min-h-0 flex-1 gap-0 overflow-hidden rounded-lg border bg-card shadow-sm"
        >
          <TabsList variant="line" aria-label="Workspace panes" className="px-4">
            <TabsTrigger value="documents">
              <FileText aria-hidden className="size-4" />
              Documents
            </TabsTrigger>
            <TabsTrigger value="chat">Chat</TabsTrigger>
          </TabsList>
          <TabsContent value="documents" className="mt-0 min-h-0 flex-1 rounded-none border-0">
            {documents}
          </TabsContent>
          <TabsContent value="chat" className="mt-0 min-h-0 flex-1 rounded-none border-0">
            {chat}
          </TabsContent>
        </Tabs>
      ) : (
        <ResizablePanelGroup
          orientation="horizontal"
          defaultLayout={panelLayout}
          onLayoutChanged={(layout) => setStoredLayout(`${layout.documents},${layout.chat}`)}
          className="min-h-0 flex-1 overflow-hidden rounded-lg border bg-card shadow-sm"
        >
          <ResizablePanel id="documents" minSize="25%">
            {documents}
          </ResizablePanel>
          <ResizableHandle withHandle />
          <ResizablePanel id="chat" minSize="35%">
            {chat}
          </ResizablePanel>
        </ResizablePanelGroup>
      )}
    </div>
  )
}
