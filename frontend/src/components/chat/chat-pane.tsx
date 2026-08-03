'use client'

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { ArrowDown, Sparkles } from 'lucide-react'
import { toast } from 'sonner'

import { Composer } from '@/components/chat/composer'
import { MessageRow, type ChatMessage } from '@/components/chat/message-bubble'
import { isProcessingStage, type ProcessingStage } from '@/components/chat/processing-state'
import { buildSuggestedPrompts } from '@/components/chat/suggested-prompts'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { ApiError, streamChat } from '@/lib/api'
import { formatCount, parseTimestamp } from '@/lib/format'
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
  /** The conversation to show; `null` falls back to the newest one. */
  sessionId?: number | null
  /** Called whenever the active conversation changes, so the URL can track it. */
  onSessionIdChange?: (sessionId: number | null) => void
  /** Rendered at the end of the pane header; the workspace owns the documents column. */
  headerActions?: React.ReactNode
}

/** How close to the bottom still counts as "following the conversation". */
const STICK_THRESHOLD_PX = 64

/** How long after a wheel, touch, or key press a scroll still counts as the reader's. */
const USER_SCROLL_WINDOW_MS = 700

/** A pause long enough that the reader will want to know when the thread resumed. */
const TIME_GAP_MS = 60 * 60 * 1000

function startsTimeGap(messages: ChatMessage[], index: number): boolean {
  if (index === 0) return true
  const previous = messages[index - 1]
  if (!previous) return false
  return (
    parseTimestamp(messages[index].created_at).getTime() -
      parseTimestamp(previous.created_at).getTime() >
    TIME_GAP_MS
  )
}

type TurnOutcome = 'active' | 'completed' | 'stopped' | 'failed'

export function ChatPane({
  classId,
  className = 'Class',
  selectedDocumentId,
  onClearSelectedDocument,
  sessionId: sessionIdProp = null,
  onSessionIdChange,
  headerActions,
}: ChatPaneProps) {
  const queryClient = useQueryClient()
  const { data: sessions, isPending: sessionsPending } = useSessions(classId)
  const createSession = useCreateSession(classId)
  const { data: settings } = useSettings()
  const { data: documents } = useDocuments(classId)
  const { data: profile } = useClassProfile(classId)
  const readyCount = documents?.filter((document) => document.state === 'ready').length ?? 0
  const disabledReason = settings?.endpoint_url
    ? null
    : 'Lyra needs a tutor endpoint before it can answer. Everything else already works offline.'

  const sessionId = sessionIdProp
  const [mode, setMode] = useState<ChatMode>('guide')
  const [draft, setDraft] = useState('')
  const [pendingTurn, setPendingTurn] = useState<ChatMessage[] | null>(null)
  const [streamText, setStreamText] = useState('')
  const [processingStage, setProcessingStage] = useState<ProcessingStage | null>(null)
  const [turnOutcome, setTurnOutcome] = useState<TurnOutcome | null>(null)
  const [revealDrained, setRevealDrained] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const creationAttemptedRef = useRef(false)
  const outcomeRef = useRef<TurnOutcome | null>(null)
  const streamTextRef = useRef('')
  const revealDrainedRef = useRef(false)
  const settledRef = useRef(false)

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
  const messages: ChatMessage[] = useMemo(() => {
    const base = persisted ?? []
    if (!pendingTurn || turnOutcome === 'failed') return base
    return [...base, ...pendingTurn]
  }, [pendingTurn, persisted, turnOutcome])

  // Only an empty class needs a new session. Existing sessions are derived directly from
  // the query result, avoiding an effect-time state update after every refetch.
  useEffect(() => {
    if (sessionId !== null || sessionsPending || !sessions || sessions.length > 0) return
    if (creationAttemptedRef.current) return
    creationAttemptedRef.current = true
    createSession.mutate(undefined, {
      onSuccess: (session) => {
        onSessionIdChange?.(session.id)
        setMode(session.mode)
      },
      onError: () => toast.error('Could not start a conversation for this class.'),
    })
  }, [createSession, onSessionIdChange, sessionId, sessions, sessionsPending])

  useEffect(() => {
    return () => abortRef.current?.abort()
  }, [])

  const clearOptimisticTurn = useCallback(() => {
    setPendingTurn(null)
    setStreamText('')
    streamTextRef.current = ''
    setProcessingStage(null)
    setTurnOutcome(null)
    outcomeRef.current = null
    setRevealDrained(false)
    revealDrainedRef.current = false
  }, [])

  const settleTurn = useCallback(
    async (immediate: boolean) => {
      if (settledRef.current) return
      settledRef.current = true
      if (immediate) clearOptimisticTurn()
      if (activeSessionId !== null) {
        await queryClient.invalidateQueries({ queryKey: chatKeys.messages(activeSessionId) })
      }
      if (!immediate) clearOptimisticTurn()
    },
    [activeSessionId, clearOptimisticTurn, queryClient],
  )

  useEffect(() => {
    let timer: number | null = null
    if (turnOutcome === 'failed') {
      timer = window.setTimeout(() => void settleTurn(true), 0)
    } else if ((turnOutcome === 'completed' || turnOutcome === 'stopped') && revealDrained) {
      timer = window.setTimeout(() => void settleTurn(false), 0)
    }
    return () => {
      if (timer !== null) window.clearTimeout(timer)
    }
  }, [revealDrained, settleTurn, turnOutcome])

  const setOutcome = useCallback((outcome: TurnOutcome) => {
    outcomeRef.current = outcome
    setTurnOutcome(outcome)
  }, [])

  const send = useCallback(
    async (content: string) => {
      if (!activeSessionId || pendingTurn !== null || content.trim().length === 0) return

      const controller = new AbortController()
      abortRef.current = controller
      settledRef.current = false
      outcomeRef.current = 'active'
      revealDrainedRef.current = false
      streamTextRef.current = ''
      setTurnOutcome('active')
      setRevealDrained(false)
      setProcessingStage('prompt_processing')

      const now = new Date().toISOString()
      let assistantText = ''

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
              streamTextRef.current = assistantText
              setStreamText(assistantText)
            } else if (event.type === 'status') {
              if (isProcessingStage(event.stage)) setProcessingStage(event.stage)
            } else if (event.type === 'notice') {
              setPendingTurn(
                (current) =>
                  current?.map((message) =>
                    message.id === -2
                      ? {
                          ...message,
                          retrieval_trimmed: event.retrieval_trimmed,
                          omitted_document_count: event.omitted_document_count,
                        }
                      : message,
                  ) ?? null,
              )
            } else if (event.type === 'done') {
              setOutcome('completed')
              if (assistantText.trim().length === 0) {
                revealDrainedRef.current = true
                setRevealDrained(true)
              }
            } else if (event.type === 'error') {
              toast.error(event.message)
              setOutcome('failed')
            }
          },
          controller.signal,
        )
      } catch (caught) {
        if (caught instanceof DOMException && caught.name === 'AbortError') {
          if (outcomeRef.current === 'active') {
            setOutcome('stopped')
            if (streamTextRef.current.trim().length === 0) {
              revealDrainedRef.current = true
              setRevealDrained(true)
            }
          }
        } else {
          toast.error(caught instanceof ApiError ? caught.message : 'The answer stopped early.')
          setOutcome('failed')
        }
      } finally {
        abortRef.current = null
        if (outcomeRef.current === 'active') {
          toast.error('The answer stopped early.')
          setOutcome('failed')
        }
      }
    },
    [activeMode, activeSessionId, pendingTurn, scopedDocument, setOutcome],
  )

  const stop = useCallback(() => {
    if (!abortRef.current) return
    setOutcome('stopped')
    if (streamTextRef.current.trim().length === 0) {
      revealDrainedRef.current = true
      setRevealDrained(true)
    }
    abortRef.current.abort()
  }, [setOutcome])

  /**
   * Called by the streaming renderer whenever its reveal queue drains. Mid-stream
   * drains (between chunks) must not count: `revealDrained` only matters once the turn
   * has ended, so the settle waits for the final words to finish fading in.
   */
  const handleRevealComplete = useCallback(() => {
    if (outcomeRef.current !== 'completed' && outcomeRef.current !== 'stopped') return
    revealDrainedRef.current = true
    setRevealDrained(true)
  }, [])

  const suggestions = useMemo(() => buildSuggestedPrompts(profile?.facts ?? []), [profile?.facts])

  const optimisticTurn = pendingTurn !== null && turnOutcome !== 'failed'
  const turnActive = pendingTurn !== null && turnOutcome === 'active'
  const rendered = optimisticTurn
    ? messages.map((message) => (message.id === -2 ? { ...message, content: streamText } : message))
    : messages

  // A conversation opens at its latest message, and follows the stream while the reader
  // is already at the tail. Scrolling up to re-read detaches the follow, and the jump
  // button is the way back.
  const viewportRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const [following, setFollowing] = useState(true)
  const followingRef = useRef(true)
  const userScrollAtRef = useRef(0)

  const scrollToBottom = useCallback((behavior: ScrollBehavior) => {
    const node = viewportRef.current
    if (!node) return
    node.scrollTo({ top: node.scrollHeight, behavior })
  }, [])

  // Only the reader can stop the conversation following its own tail. Content that grows
  // under a scroll that already ran (KaTeX re-laying out, the documents column opening)
  // also leaves the viewport short of the bottom, and treating that as "the reader
  // scrolled up" strands them mid-message with no way back except the jump button.
  useEffect(() => {
    const node = viewportRef.current
    if (!node) return

    const noteUserScroll = () => {
      userScrollAtRef.current = performance.now()
    }
    const onScroll = () => {
      const distance = node.scrollHeight - node.scrollTop - node.clientHeight
      const atBottom = distance <= STICK_THRESHOLD_PX
      // Arriving at the bottom always resumes following, however it happened. Leaving it
      // only counts when the reader drove it.
      const byUser = performance.now() - userScrollAtRef.current < USER_SCROLL_WINDOW_MS
      if (!atBottom && !byUser) return
      followingRef.current = atBottom
      setFollowing(atBottom)
    }

    node.addEventListener('scroll', onScroll, { passive: true })
    node.addEventListener('wheel', noteUserScroll, { passive: true })
    node.addEventListener('touchmove', noteUserScroll, { passive: true })
    node.addEventListener('keydown', noteUserScroll)
    return () => {
      node.removeEventListener('scroll', onScroll)
      node.removeEventListener('wheel', noteUserScroll)
      node.removeEventListener('touchmove', noteUserScroll)
      node.removeEventListener('keydown', noteUserScroll)
    }
  }, [])

  // Opening a conversation jumps straight to the end, with no visible travel through
  // history the reader did not ask to see.
  useLayoutEffect(() => {
    if (messagesPending) return
    followingRef.current = true
    scrollToBottom('instant')
  }, [activeSessionId, messagesPending, scrollToBottom])

  // A new message animates into view; streamed tokens do not, because a smooth scroll
  // restarting on every token never arrives.
  useEffect(() => {
    if (!followingRef.current) return
    scrollToBottom('smooth')
  }, [rendered.length, scrollToBottom])

  useEffect(() => {
    if (!followingRef.current || !optimisticTurn) return
    scrollToBottom('instant')
  }, [streamText, processingStage, optimisticTurn, scrollToBottom])

  // The conversation keeps growing after the first paint: KaTeX re-lays out its math,
  // and opening the documents column reflows every paragraph taller. Both move the tail
  // out from under a scroll that already ran, so watch the content itself and re-pin.
  // Only while following, so this never yanks a reader who scrolled up to re-read.
  useEffect(() => {
    const node = contentRef.current
    if (!node || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => {
      if (followingRef.current) scrollToBottom('instant')
    })
    observer.observe(node)
    return () => observer.disconnect()
  }, [scrollToBottom])

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-3 border-b px-4">
        {/* Below the desktop layout a Chat tab already names this pane, so repeating
            "Tutor" underneath it spends a second row of a small screen saying nothing. */}
        <h2 className="hidden text-xs font-medium tracking-[0.14em] uppercase lg:block">Tutor</h2>
        {/* Right-aligned beside the Tutor label on desktop; on compact the label is gone,
            so it sits left under the Chat tab rather than stranded across empty space. */}
        <div
          className="flex items-stretch self-stretch lg:ml-auto"
          role="group"
          aria-label="Answer style"
        >
          {MODES.map((option) => (
            <Tooltip key={option.value}>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-pressed={activeMode === option.value}
                  className={cn(
                    'relative h-9 rounded-none border-b-2 border-transparent px-3 text-xs after:absolute after:inset-x-3 after:bottom-[-1px] after:h-0.5 after:bg-accent-primary after:opacity-0 lg:h-10',
                    activeMode === option.value
                      ? 'text-foreground after:opacity-100 hover:bg-transparent'
                      : 'text-text-secondary hover:bg-transparent hover:text-foreground',
                  )}
                  onClick={() => {
                    if (sessionId === null && activeSessionId !== null) {
                      onSessionIdChange?.(activeSessionId)
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
        {headerActions ? (
          <div className="flex shrink-0 items-center gap-1 pl-1">{headerActions}</div>
        ) : null}
      </div>

      <div className="relative min-h-0 flex-1">
        <ScrollArea viewportRef={viewportRef} className="h-full">
          <div ref={contentRef} className="mx-auto max-w-[860px] space-y-6 p-4 md:px-6">
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
                  startsTimeGap={startsTimeGap(rendered, index)}
                  streaming={optimisticTurn && message.id === -2}
                  processingStage={optimisticTurn && message.id === -2 ? processingStage : null}
                  turnEnded={
                    optimisticTurn && message.id === -2
                      ? turnOutcome === 'completed' || turnOutcome === 'stopped'
                      : undefined
                  }
                  onRevealComplete={
                    optimisticTurn && message.id === -2 ? handleRevealComplete : undefined
                  }
                  canRetry={
                    !optimisticTurn && message.role === 'assistant' && index === rendered.length - 1
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

        {!following && rendered.length > 0 ? (
          <Button
            variant="outline"
            size="sm"
            onClick={() => scrollToBottom('smooth')}
            className="absolute inset-x-0 bottom-3 mx-auto w-fit rounded-full shadow-md"
          >
            <ArrowDown className="size-3.5" />
            Jump to latest
          </Button>
        ) : null}
      </div>

      <div className="shrink-0 border-t bg-card p-4 md:px-6">
        <div className="mx-auto max-w-[860px]">
          <Composer
            value={draft}
            onChange={setDraft}
            onSend={() => void send(draft)}
            onStop={stop}
            streaming={turnActive}
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
    <div className="flex min-h-[60vh] flex-col justify-center py-8">
      <div className="bg-accent-surface text-accent-surface-foreground mb-4 flex size-10 items-center justify-center rounded-md">
        <Sparkles className="size-5" aria-hidden />
      </div>
      <h2 className="text-2xl font-medium">{className}</h2>
      <p className="text-text-secondary mt-1 text-sm">
        {readyCount === 0
          ? 'Nothing indexed yet. Upload a document and Lyra will have something to work from.'
          : `${formatCount(readyCount, 'document')} indexed${hasProfile ? ', syllabus analyzed' : ''}.`}
      </p>
      <p className="text-text-tertiary mt-6 mb-2 text-xs font-medium tracking-[0.14em] uppercase">
        Try asking
      </p>
      <div className="flex flex-col items-start gap-2">
        {suggestions.map((prompt) => (
          <Button
            key={prompt}
            variant="outline"
            size="sm"
            className="h-auto max-w-full py-2 text-left whitespace-normal"
            onClick={() => onPick(prompt)}
          >
            {prompt}
          </Button>
        ))}
      </div>
    </div>
  )
}
