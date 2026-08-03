'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'

import { Composer } from '@/components/chat/composer'
import { MessageRow, type ChatMessage } from '@/components/chat/message-bubble'
import { buildSuggestedPrompts } from '@/components/chat/suggested-prompts'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { ApiError, streamChat } from '@/lib/api'
import { formatCount } from '@/lib/format'
import { chatKeys, useCreateSession, useMessages, useSessions } from '@/lib/hooks/use-chat'
import { useDocuments } from '@/lib/hooks/use-documents'
import { useClassProfile } from '@/lib/hooks/use-profile'
import { useSettings } from '@/lib/hooks/use-settings'
import { cn } from '@/lib/utils'
import type { ChatMode } from '@/types'

const MODES: { value: ChatMode; label: string; hint: string }[] = [
  {
    value: 'guide',
    label: 'Guide',
    hint: 'Lyra asks leading questions and holds back the answer.',
  },
  { value: 'show', label: 'Show', hint: 'Lyra explains the full solution directly.' },
]

type ChatPaneProps = {
  classId: number
  className?: string
  selectedDocumentId: number | null
  onClearSelectedDocument: () => void
}

export function ChatPane({
  classId,
  className = 'Class',
  selectedDocumentId,
  onClearSelectedDocument,
}: ChatPaneProps) {
  const queryClient = useQueryClient()
  const { data: sessions, isPending: sessionsPending } = useSessions(classId)
  const createSession = useCreateSession(classId)
  const { data: settings } = useSettings()
  const { data: documents } = useDocuments(classId)
  const { data: profile } = useClassProfile(classId)

  const [sessionId, setSessionId] = useState<number | null>(null)
  const [mode, setMode] = useState<ChatMode>('guide')
  const [draft, setDraft] = useState('')
  const [pendingTurn, setPendingTurn] = useState<ChatMessage[] | null>(null)
  const [streamText, setStreamText] = useState('')
  const abortRef = useRef<AbortController | null>(null)
  const creationAttemptedRef = useRef(false)

  const newestSession = useMemo(
    () => (sessions && sessions.length > 0 ? [...sessions].sort((a, b) => b.id - a.id)[0] : null),
    [sessions],
  )
  const activeSessionId = sessionId ?? newestSession?.id ?? null
  const activeMode = sessionId === null ? (newestSession?.mode ?? mode) : mode
  const scopedDocument = useMemo(
    () => documents?.find((document) => document.id === selectedDocumentId) ?? null,
    [documents, selectedDocumentId],
  )

  const { data: persisted, isPending: messagesPending } = useMessages(activeSessionId)

  // Only an empty class needs a new session. Existing sessions are derived directly from
  // the query result, avoiding an effect-time state update after every refetch.
  useEffect(() => {
    if (sessionId !== null || sessionsPending || !sessions || sessions.length > 0) return
    if (creationAttemptedRef.current) return
    creationAttemptedRef.current = true
    createSession.mutate(undefined, {
      onSuccess: (session) => {
        setSessionId(session.id)
        setMode(session.mode)
      },
      onError: () => toast.error('Could not start a conversation for this class.'),
    })
  }, [createSession, sessionId, sessions, sessionsPending])

  useEffect(() => {
    return () => abortRef.current?.abort()
  }, [])

  const messages: ChatMessage[] = useMemo(() => {
    const base = persisted ?? []
    if (!pendingTurn) return base
    return [...base, ...pendingTurn]
  }, [persisted, pendingTurn])

  const readyCount = documents?.filter((document) => document.state === 'ready').length ?? 0
  const disabledReason = settings?.endpoint_url
    ? null
    : 'Lyra needs a tutor endpoint before it can answer. Everything else already works offline.'

  const send = useCallback(
    async (content: string) => {
      if (!activeSessionId || content.trim().length === 0) return

      const controller = new AbortController()
      abortRef.current = controller
      const now = new Date().toISOString()
      let assistantText = ''
      let trimmed = false
      let omitted = 0

      setDraft('')
      setStreamText('')
      setPendingTurn([
        {
          id: -1,
          role: 'user',
          content: content.trim(),
          retrieval_trimmed: false,
          omitted_document_count: 0,
          created_at: now,
        },
        {
          id: -2,
          role: 'assistant',
          content: '',
          retrieval_trimmed: false,
          omitted_document_count: 0,
          created_at: now,
        },
      ])

      try {
        await streamChat(
          activeSessionId,
          { content: content.trim(), mode: activeMode, document_id: scopedDocument?.id ?? null },
          (event) => {
            if (event.type === 'token') {
              assistantText += event.text
              setStreamText(assistantText)
            } else if (event.type === 'notice') {
              trimmed = event.retrieval_trimmed
              omitted = event.omitted_document_count
            } else if (event.type === 'error') {
              toast.error(event.message)
            }
          },
          controller.signal,
        )
      } catch (caught) {
        if (!(caught instanceof DOMException && caught.name === 'AbortError')) {
          toast.error(caught instanceof ApiError ? caught.message : 'The answer stopped early.')
        }
      } finally {
        abortRef.current = null
        // The backend persisted both rows, so refetching is what makes them real. The local
        // turn is cleared only after that lands, otherwise the answer would blink away.
        await queryClient.invalidateQueries({ queryKey: chatKeys.messages(activeSessionId) })
        setPendingTurn(null)
        setStreamText('')
        void trimmed
        void omitted
      }
    },
    [activeMode, activeSessionId, queryClient, scopedDocument],
  )

  const suggestions = useMemo(() => buildSuggestedPrompts(profile?.facts ?? []), [profile?.facts])

  const streaming = pendingTurn !== null
  const rendered = streaming
    ? messages.map((message) => (message.id === -2 ? { ...message, content: streamText } : message))
    : messages

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between gap-3 border-b px-4">
        <h2 className="text-xs font-medium tracking-[0.14em] uppercase">Tutor</h2>
        <div className="flex items-stretch self-stretch" role="group" aria-label="Answer style">
          {MODES.map((option) => (
            <Tooltip key={option.value}>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-pressed={activeMode === option.value}
                  className={cn(
                    'relative h-10 rounded-none border-b-2 border-transparent px-3 text-xs after:absolute after:inset-x-3 after:bottom-[-1px] after:h-0.5 after:bg-accent-primary after:opacity-0',
                    activeMode === option.value
                      ? 'text-foreground after:opacity-100 hover:bg-transparent'
                      : 'text-text-secondary hover:bg-transparent hover:text-foreground',
                  )}
                  onClick={() => {
                    if (sessionId === null && activeSessionId !== null) {
                      setSessionId(activeSessionId)
                    }
                    setMode(option.value)
                  }}
                >
                  {option.label}
                </Button>
              </TooltipTrigger>
              <TooltipContent>{option.hint}</TooltipContent>
            </Tooltip>
          ))}
        </div>
      </div>

      <ScrollArea className="min-h-0 flex-1">
        <div className="mx-auto max-w-[720px] space-y-6 p-4">
          {sessionsPending || messagesPending ? (
            <div className="space-y-4" aria-busy="true" aria-label="Loading conversation">
              <Skeleton className="ml-auto h-12 w-2/3" />
              <Skeleton className="h-24 w-full" />
            </div>
          ) : rendered.length === 0 ? (
            <EmptyConversation
              className={className}
              readyCount={readyCount}
              hasProfile={(profile?.facts.length ?? 0) > 0}
              suggestions={suggestions}
              onPick={setDraft}
            />
          ) : (
            rendered.map((message, index) => (
              <MessageRow
                key={message.id}
                message={message}
                streaming={streaming && message.id === -2}
                canRetry={
                  !streaming && message.role === 'assistant' && index === rendered.length - 1
                }
                onRetry={() => {
                  const previous = rendered[index - 1]
                  if (previous?.role === 'user') void send(previous.content)
                }}
              />
            ))
          )}
        </div>
      </ScrollArea>

      <div className="border-t bg-card p-4">
        <div className="mx-auto max-w-[720px]">
          <Composer
            value={draft}
            onChange={setDraft}
            onSend={() => void send(draft)}
            onStop={() => abortRef.current?.abort()}
            streaming={streaming}
            disabledReason={
              activeSessionId === null ? 'Opening this conversation...' : disabledReason
            }
            scopedDocumentName={scopedDocument?.filename ?? null}
            onClearScope={onClearSelectedDocument}
          />
        </div>
      </div>
    </div>
  )
}

type EmptyConversationProps = {
  className: string
  readyCount: number
  hasProfile: boolean
  suggestions: string[]
  onPick: (prompt: string) => void
}

function EmptyConversation({
  className,
  readyCount,
  hasProfile,
  suggestions,
  onPick,
}: EmptyConversationProps) {
  return (
    <div className={cn('space-y-4 py-8')}>
      <div>
        <h2 className="text-xl font-medium">{className}</h2>
        <p className="text-text-secondary text-sm">
          {readyCount === 0
            ? 'Nothing indexed yet. Upload a document and Lyra will have something to work from.'
            : `${formatCount(readyCount, 'document')} indexed${hasProfile ? ', syllabus analyzed' : ''}.`}
        </p>
      </div>
      <div className="flex flex-col items-start gap-2">
        {suggestions.map((prompt) => (
          <Button key={prompt} variant="outline" size="sm" onClick={() => onPick(prompt)}>
            {prompt}
          </Button>
        ))}
      </div>
    </div>
  )
}
