'use client'

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { ArrowDown } from 'lucide-react'
import { toast } from 'sonner'

import { Composer } from '@/components/chat/composer'
import { LyraMark } from '@/components/chat/lyra-mark'
import { Asterism } from '@/components/ui/asterism'
import { HeaderActions } from '@/components/layout/page-chrome'
import { MessageRow, type ChatMessage } from '@/components/chat/message-bubble'
import { isProcessingStage, type ProcessingStage } from '@/components/chat/thinking-indicator'
import { buildSuggestedPrompts } from '@/components/chat/suggested-prompts'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import {
  ApiError,
  api,
  streamChat,
  streamChatRetry,
  streamRegenerate,
  streamWriterChat,
  streamWriterChatRetry,
} from '@/lib/api'
import { formatCount, parseTimestamp } from '@/lib/format'
import { chatKeys, useCreateSession, useMessages, useSessions } from '@/lib/hooks/use-chat'
import { invalidateAgentTurnCaches } from '@/lib/hooks/use-agent'
import { useDocuments } from '@/lib/hooks/use-documents'
import { useMediaQuery } from '@/lib/hooks/use-media-query'
import { useClassProfile } from '@/lib/hooks/use-profile'
import { useSettings } from '@/lib/hooks/use-settings'
import { cn } from '@/lib/utils'
import type { ChatEvent, ChatMode, WriterActivity } from '@/types'

const MODES: { value: ChatMode; label: string; hint: string }[] = [
  {
    value: 'guide',
    label: 'Guide me',
    hint: 'Lyra works toward your understanding - explaining, scaffolding, checking in - at a pace you can follow.',
  },
  {
    value: 'show',
    label: 'Show solution',
    hint: 'Lyra works through the full answer, start to finish.',
  },
]

/**
 * The pane in the draft workspace's rail: same conversation surface, a different
 * answerer. No Guide/Show - there is one writer - and turns go to the writer endpoint,
 * which narrates its tool calls as activity frames and reports landed side effects.
 */
type WriterVariant = {
  artifactId: number
  /** A proposal landed mid-turn; the workspace flips the rail to the suggestion. */
  onProposed?: (editId: number) => void
  /** The assistant recorded its guess at the brief; the card above should refetch. */
  onBrief?: () => void
  /** The assistant queued a draft pass; the workspace's status poll should notice. */
  onPass?: () => void
  /** The assistant queued a review; the status poll and the Comments tab should notice. */
  onReview?: () => void
  /** The assistant replied under comment threads; the Comments tab should refetch. */
  onComments?: () => void
}

type ChatPaneProps = {
  classId: number
  className?: string
  selectedDocumentId: number | null
  /** Present in the draft workspace: this pane speaks to the writer, not the tutor. */
  writer?: WriterVariant
  /**
   * The ordinary class conversation speaks to the contextual agent, not the tutor:
   * turns go to the non-streaming agent endpoint, which plans the work itself and
   * answers in place. The agent's just-in-time work (access requests, edits to review,
   * commands to approve, the activity trail) is surfaced by the surrounding
   * `AgentWorkSurface`, which re-fetches when the transcript grows.
   */
  agent?: boolean
  /** The conversation to show; `null` falls back to the newest one. */
  sessionId?: number | null
  /**
   * Start a fresh conversation rather than falling back to the newest.
   *
   * Nothing is created on the server until the first message is sent. An empty chat is a
   * click, not history, and creating one up front is what used to fill the rail with
   * untitled conversations nobody had said anything in.
   */
  draft?: boolean
  /**
   * A question carried in from somewhere else: the class landing's ask box, a quiz miss,
   * a weak topic on a results screen. Placed in the composer on arrival, so the student
   * sees the words before they go anywhere.
   */
  initialAsk?: string | null
  /**
   * Send `initialAsk` immediately instead of leaving it in the composer. Only for the
   * student's own words, typed somewhere that hands off to here: they already asked, and
   * making them press Enter twice would be the page charging a toll for moving.
   */
  initialSend?: boolean
  /**
   * The step of a solution a newly opened conversation is about.
   *
   * Pins that step into every turn. It is what makes asking about step 2 a conversation
   * rather than a fresh question that happens to quote one.
   */
  anchorPartId?: number | null
  /**
   * `pane` fills its column and scrolls itself. `inline` flows into whatever is around it
   * and follows the stream in the scroll container named by `scrollViewportRef`, which is
   * what lets a conversation open underneath the step it is about without covering it.
   */
  layout?: 'pane' | 'inline' | 'assistant'
  /** The scrolling ancestor to follow, required by `inline`. */
  scrollViewportRef?: React.RefObject<HTMLDivElement | null>
  /** Called whenever the active conversation changes, so the URL can track it. */
  onSessionIdChange?: (sessionId: number | null) => void
  /** Rendered at the end of the pane header; the workspace owns the documents column. */
  headerActions?: React.ReactNode
  /**
   * The empty-state copy, when the class-level one does not fit. A conversation anchored
   * to a step of a solution opens with the step already on screen, so suggesting "What
   * are the main topics in this class?" would be answering a question nobody asked.
   */
  emptyState?: React.ReactNode
  /**
   * The active source context, rendered in the composer's control row. A writer variant
   * passes nothing - the writer reads the class, not a pick.
   */
  sourceControl?: React.ReactNode
  /**
   * The attached local workspace for this class conversation, rendered beside the source
   * context in the composer's control row - the same compact chip, so the two answers to
   * "what does Lyra have on hand" sit together.
   */
  workspaceControl?: React.ReactNode
}

/** How close to the bottom still counts as "following the conversation". */
const STICK_THRESHOLD_PX = 64

/** How long after a wheel, touch, or key press a scroll still counts as the reader's. */
const USER_SCROLL_WINDOW_MS = 700

/** A pause long enough that the reader will want to know when the thread resumed. */
const TIME_GAP_MS = 60 * 60 * 1000

// A settling Stop's status poll: a calm interval, a generous bound. The server keeps the
// stopped turn's request (and its connection) open only as long as its late workers
// remain, and those workers bound their own work (network timeouts, the command
// ceiling) - so the proof of a free session always arrives well inside this bound. The
// bound itself is a safety net that exceeds even the slowest of them; past it the pane
// stops claiming anything and hands the Stop affordance back to the student.
const _STOP_SETTLE_POLL_MS = 750
const _STOP_SETTLE_LIMIT_MS = 12 * 60 * 1000

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

/** A new question, a tutor regeneration, a tutor retry (PLA-306), or a writer retry (PLA-310). */
type TurnKind = 'send' | 'regenerate' | 'tutor-retry' | 'writer-retry'

export function ChatPane({
  classId,
  className = 'Class',
  selectedDocumentId,
  writer,
  agent = false,
  sessionId: sessionIdProp = null,
  draft: isDraft = false,
  initialAsk = null,
  initialSend = false,
  anchorPartId = null,
  layout = 'pane',
  scrollViewportRef,
  onSessionIdChange,
  headerActions,
  emptyState,
  sourceControl,
  workspaceControl,
}: ChatPaneProps) {
  const inline = layout === 'inline'
  // Matches the workspace's own desktop breakpoint, so the controls move into the header
  // exactly when the Chat/Documents tab bar stops existing.
  const wide = useMediaQuery('(min-width: 1024px)')
  const queryClient = useQueryClient()
  const {
    data: sessions,
    isPending: sessionsPending,
    isError: sessionsError,
    refetch: retrySessions,
  } = useSessions(classId)
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
  const [draft, setDraft] = useState(initialAsk ?? '')
  // Async turn cleanup may restore its submitted text only until the reader edits again.
  // A revision also protects an intentionally empty follow-up from being overwritten.
  const draftRevisionRef = useRef(0)
  const changeDraft = useCallback((value: string) => {
    draftRevisionRef.current += 1
    setDraft(value)
  }, [])
  const sendingRef = useRef<symbol | null>(null)
  const draftScopeVersionRef = useRef(0)
  // Only session creation may authorize a null → ID transition. A running turn's
  // session ID is also used when manually returning to it, so it cannot prove a handoff.
  const expectedHandoffRef = useRef<{ sessionId: number; scopeVersion: number } | null>(null)
  const [sending, setSending] = useState(false)
  const [pendingTurn, setPendingTurn] = useState<ChatMessage[] | null>(null)
  const [turnBase, setTurnBase] = useState<ChatMessage[] | null>(null)
  const [streamText, setStreamText] = useState('')
  const [streamThinking, setStreamThinking] = useState('')
  const [streamActivity, setStreamActivity] = useState<WriterActivity[]>([])
  const [turnStartedAt, setTurnStartedAt] = useState<number | null>(null)
  const [processingStage, setProcessingStage] = useState<ProcessingStage | null>(null)
  const [turnOutcome, setTurnOutcome] = useState<TurnOutcome | null>(null)
  const [turnKind, setTurnKind] = useState<TurnKind>('send')
  const [revealDrained, setRevealDrained] = useState(false)
  /**
   * The pane has been pointed at a different conversation than the turn in flight.
   *
   * The turn keeps streaming; it just has nowhere to show. Set from a change in which
   * conversation is on screen rather than from comparing the two — while a first message
   * is in the air the pane is still the draft it was sent from, and the URL only catches
   * up once the conversation exists.
   */
  const [detached, setDetached] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const outcomeRef = useRef<TurnOutcome | null>(null)
  /**
   * The conversation the turn in flight was sent to.
   *
   * A turn outlives the render that started it, and the pane can be pointed somewhere
   * else while it is still streaming, so which conversation it belongs to cannot be read
   * back off the props. Held from the moment the turn has a conversation to belong to —
   * inside `ensureSession`, before the URL is told about it.
   */
  const turnSessionRef = useRef<number | null>(null)
  /**
   * Which turn owns the pane. Starting a turn somewhere else leaves the previous one
   * running with nothing to draw on: the answer is still being written and the server
   * still stores it, so cutting it off would throw away work the student asked for.
   */
  const turnIdRef = useRef(0)
  const streamTextRef = useRef('')
  const revealDrainedRef = useRef(false)
  const settledRef = useRef(false)
  // Read inside the stream callback, which closes over the value it was created with, so
  // the elapsed thinking time cannot come from the `turnStartedAt` state.
  const turnOpenedAtRef = useRef(0)
  const submittedTextRef = useRef<string | null>(null)
  const operationIdRef = useRef<string | null>(null)
  const responseAcceptedRef = useRef(false)
  // Whether the turn in flight is a contextual agent turn. Stop must do more than abort a
  // local fetch for these: the non-streaming handler cannot see its client's disconnect,
  // so the server-side cancellation (attempt settled, claim released) is explicit.
  const agentTurnRef = useRef(false)
  // The agent's Stop is a round trip: the composer shows "Stopping…" while /agent-chat/stop
  // is in flight, and the terminal stopped state is presented only once the server has
  // confirmed it - never faked locally.
  const [stopping, setStopping] = useState(false)
  const stopInFlightRef = useRef(false)

  const newestSession = useMemo(
    () => (sessions && sessions.length > 0 ? [...sessions].sort((a, b) => b.id - a.id)[0] : null),
    [sessions],
  )
  // The writer's conversation is the draft's, never the class's: falling back to the
  // newest tutor session here would put the tutor's transcript in the writer's rail.
  const activeSessionId = writer
    ? sessionId
    : isDraft
      ? null
      : (sessionId ?? newestSession?.id ?? null)
  const newestMode = newestSession?.mode
  const activeMode =
    sessionId === null && !isDraft && !writer && newestMode !== 'writer'
      ? (newestMode ?? mode)
      : mode
  const scopedDocument = useMemo(
    () => documents?.find((document) => document.id === selectedDocumentId) ?? null,
    [documents, selectedDocumentId],
  )

  const {
    data: persisted,
    isPending: messagesQueryPending,
    isError: messagesError,
    refetch: retryMessages,
  } = useMessages(activeSessionId)
  const sessionListNeeded = !writer && !isDraft && sessionId === null
  const historyError =
    (sessionListNeeded && sessionsError) || (activeSessionId !== null && messagesError)
  /** A turn is in flight *and* it belongs to the conversation being read. */
  const showingTurn = !detached && pendingTurn !== null
  // A conversation that does not exist yet is not loading. Without this the query sits at
  // `pending` forever, because it is disabled, and a new chat would show a skeleton
  // instead of somewhere to type.
  // Neither is one whose turn is already on screen. Sending the first message is what
  // brings the conversation into being, which is what first enables this query — so the
  // skeleton used to cut in between the empty state and the question, for exactly as long
  // as the first fetch took. That is the blink after picking a suggested prompt.
  const messagesPending = activeSessionId !== null && messagesQueryPending && !showingTurn
  const messages: ChatMessage[] = useMemo(() => {
    const live = persisted ?? []
    if (!showingTurn || !pendingTurn || turnOutcome === 'failed') return live
    // A turn is appended to the transcript it started from, not to whatever the message
    // query happens to be holding mid-stream. The server stores the question the moment the
    // turn opens, and on the first message of a new conversation that write races the first
    // fetch of the message list — which only becomes possible once the conversation exists.
    // Reading `live` here meant the fetch came back with the question already in it and the
    // student watched their own question sit on screen twice until the turn settled.
    const base = turnBase ?? live
    // A retry answers the question already on screen, so its optimistic reply stands where
    // the previous one did rather than below it. The server holds the old reply until the
    // new one is written, which is why a failed retry falls back to `live` intact.
    if (turnKind === 'regenerate' || turnKind === 'tutor-retry' || turnKind === 'writer-retry') {
      const withoutLastReply =
        base.length > 0 && base[base.length - 1].role === 'assistant' ? base.slice(0, -1) : base
      return [...withoutLastReply, ...pendingTurn]
    }
    return [...base, ...pendingTurn]
  }, [pendingTurn, persisted, showingTurn, turnBase, turnKind, turnOutcome])

  /**
   * The conversation this turn belongs to, opening one if there is not one yet.
   *
   * Sending is the moment a conversation starts existing. Opening the pane, clicking New
   * chat, and browsing away again all used to create a row, which is why a class ended up
   * with a rail full of chats containing nothing.
   */
  const ensureSession = useCallback(
    async (owns: () => boolean): Promise<number | null> => {
      if (activeSessionId !== null) return activeSessionId
      try {
        if (writer) {
          const session = await api.createWriterSession(writer.artifactId)
          if (!owns()) return null
          turnSessionRef.current = session.id
          expectedHandoffRef.current = {
            sessionId: session.id,
            scopeVersion: draftScopeVersionRef.current,
          }
          onSessionIdChange?.(session.id)
          return session.id
        }
        const session = await createSession.mutateAsync(anchorPartId)
        if (!owns()) return null
        turnSessionRef.current = session.id
        expectedHandoffRef.current = {
          sessionId: session.id,
          scopeVersion: draftScopeVersionRef.current,
        }
        onSessionIdChange?.(session.id)
        if (session.mode !== 'writer') setMode(session.mode)
        return session.id
      } catch {
        toast.error(
          writer
            ? 'Could not start a conversation for this draft.'
            : 'Could not start a conversation for this class.',
        )
        return null
      }
    },
    [activeSessionId, anchorPartId, createSession, onSessionIdChange, writer],
  )

  useEffect(() => {
    return () => {
      // Session creation can still be pending before a stream/controller exists.
      // Revoke that send before its response can navigate or start an unmounted pane.
      sendingRef.current = null
      expectedHandoffRef.current = null
      abortRef.current?.abort()
    }
  }, [])

  const clearOptimisticTurn = useCallback(() => {
    // Nothing on screen belongs to a particular conversation any more, so leaving one is
    // no longer something that puts anything away.
    turnSessionRef.current = null
    setDetached(false)
    setPendingTurn(null)
    setTurnBase(null)
    setStreamText('')
    setStreamThinking('')
    setStreamActivity([])
    setTurnStartedAt(null)
    streamTextRef.current = ''
    setProcessingStage(null)
    setTurnOutcome(null)
    outcomeRef.current = null
    setRevealDrained(false)
    revealDrainedRef.current = false
    stopInFlightRef.current = false
    setStopping(false)
  }, [])

  /**
   * Which conversation the pane is showing, and whether the turn in flight is still in it.
   *
   * Clicking New chat mid-answer used to change the URL and leave the previous
   * conversation and its half-written reply on screen until the turn settled — only then
   * did the empty chat appear. The optimistic rows had nothing tying them to the
   * conversation they belonged to, so pointing the pane elsewhere did not put them away.
   *
   * Only the answer's *place* moves: it goes on streaming, is still written to the
   * conversation it was asked in, and comes back on screen mid-sentence when the student
   * returns to it. Layout, not passive: a frame of the old conversation under a click
   * that was meant to leave it is the flicker this is here to prevent.
   */
  // Drafts belong to the displayed conversation and are cleared when navigating away.
  // The first send assigning an ID to its own draft is a handoff, not navigation.
  const draftScopeRef = useRef({
    classId,
    artifactId: writer?.artifactId,
    sessionId: activeSessionId,
  })
  useLayoutEffect(() => {
    const previous = draftScopeRef.current
    const sameContext = previous.classId === classId && previous.artifactId === writer?.artifactId
    if (sameContext && previous.sessionId === activeSessionId) return
    draftScopeRef.current = { classId, artifactId: writer?.artifactId, sessionId: activeSessionId }
    const handoff = expectedHandoffRef.current
    // Consume once, even when navigation went somewhere other than the created session.
    expectedHandoffRef.current = null
    if (
      sameContext &&
      previous.sessionId === null &&
      activeSessionId === handoff?.sessionId &&
      handoff.scopeVersion === draftScopeVersionRef.current
    )
      return
    draftScopeVersionRef.current += 1
    sendingRef.current = null
    setSending(false)
    changeDraft('')
  }, [activeSessionId, classId, writer?.artifactId, changeDraft])

  const shownSessionRef = useRef(activeSessionId)
  useLayoutEffect(() => {
    if (shownSessionRef.current === activeSessionId) return
    shownSessionRef.current = activeSessionId
    // No turn to place. A first message is sent from a draft and only then gets a
    // conversation, so `null` here also covers the window before the URL catches up: the
    // pane has not gone anywhere, and nothing about the turn has changed hands.
    if (turnSessionRef.current === null) return
    setDetached(activeSessionId !== turnSessionRef.current)
  }, [activeSessionId])

  const settleTurn = useCallback(
    async (immediate: boolean) => {
      if (settledRef.current) return
      settledRef.current = true
      // The conversation the turn was sent to, which is not always the one on screen: a
      // turn can settle after the pane has moved on, and refetching whatever is being read
      // now would leave the answer's own transcript holding a stale copy of itself.
      const turnSessionId = turnSessionRef.current
      const owner = turnIdRef.current
      if (immediate) clearOptimisticTurn()
      if (turnSessionId !== null) {
        await queryClient.invalidateQueries({ queryKey: chatKeys.messages(turnSessionId) })
      }
      if (!immediate && turnIdRef.current === owner) clearOptimisticTurn()
    },
    [clearOptimisticTurn, queryClient],
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

  /** The blank assistant row a turn streams into. */
  const placeholderReply = useCallback(
    (createdAt: string): ChatMessage => ({
      id: -2,
      role: 'assistant',
      content: '',
      thinking: '',
      thinking_ms: 0,
      retrieval_trimmed: false,
      omitted_document_count: 0,
      tool_activity: [],
      created_at: createdAt,
    }),
    [],
  )

  /**
   * One turn, whether it is a new question or a second attempt at the last one. Both read
   * the same frame protocol and settle the same way; they differ only in what they send
   * and in which optimistic rows stand in for the answer while it streams.
   */
  const runTurn = useCallback(
    async (kind: TurnKind, content: string, turnSessionId: number, restoreDraft?: () => void) => {
      // One question at a time in a conversation — but only in that conversation. A turn
      // still running in the chat the student just left is not a reason the chat they
      // opened instead cannot be typed in, which is what `detached` says about it.
      if (pendingTurn !== null && !detached) return

      // Whatever was running is now unwatched: it keeps streaming into the conversation it
      // was asked in, and stops writing to a pane that has become someone else's.
      const turnId = turnIdRef.current + 1
      turnIdRef.current = turnId
      const owns = () => turnIdRef.current === turnId

      const controller = new AbortController()
      abortRef.current = controller
      turnSessionRef.current = turnSessionId
      setDetached(false)
      settledRef.current = false
      agentTurnRef.current = Boolean(agent)
      outcomeRef.current = 'active'
      revealDrainedRef.current = false
      streamTextRef.current = ''
      setTurnKind(kind)
      setTurnOutcome('active')
      setRevealDrained(false)
      setProcessingStage('prompt_processing')
      turnOpenedAtRef.current = performance.now()
      setTurnStartedAt(turnOpenedAtRef.current)

      const now = new Date().toISOString()
      let assistantText = ''
      let reasoningText = ''

      setStreamText('')
      setStreamThinking('')
      setStreamActivity([])
      // Fixed for the length of the turn, and taken from the render the student sent from:
      // for a conversation that did not exist a moment ago that is an empty transcript,
      // which is exactly what this turn is being appended to.
      setTurnBase(persisted ?? [])
      setPendingTurn(
        kind === 'regenerate' || kind === 'tutor-retry' || kind === 'writer-retry'
          ? [placeholderReply(now)]
          : [
              {
                id: -1,
                role: 'user',
                content,
                thinking: '',
                thinking_ms: 0,
                retrieval_trimmed: false,
                omitted_document_count: 0,
                tool_activity: [],
                created_at: now,
              },
              placeholderReply(now),
            ],
      )

      const onEvent = (event: ChatEvent) => {
        // A turn the pane has handed on still has an answer coming, and the server still
        // writes it down. It just has no rows of its own to put it in any more.
        if (!owns()) return
        if (event.type === 'token') {
          assistantText += event.text
          streamTextRef.current = assistantText
          setStreamText(assistantText)
        } else if (event.type === 'reasoning') {
          reasoningText += event.text
          setStreamThinking(reasoningText)
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
        } else if (event.type === 'activity') {
          setStreamActivity((current) => [
            ...current,
            { tool: event.tool, label: event.label, ok: event.ok },
          ])
        } else if (event.type === 'proposed') {
          writer?.onProposed?.(event.edit_id)
        } else if (event.type === 'brief') {
          writer?.onBrief?.()
        } else if (event.type === 'pass') {
          writer?.onPass?.()
        } else if (event.type === 'review') {
          writer?.onReview?.()
        } else if (event.type === 'comments') {
          writer?.onComments?.()
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
      }

      try {
        if (agent) {
          // Feed live agent deltas through the same reasoning and reveal path as tutor
          // turns. A new tool round replaces its intermediate prose, retaining reasoning.
          const onAgentEvent = (event: import('@/lib/api').AgentStreamEvent) => {
            if (!owns()) return
            if (event.type === 'reset') {
              assistantText = ''
              streamTextRef.current = ''
              setStreamText('')
            } else {
              onEvent(event)
            }
          }
          const agentDocumentId = scopedDocument?.id ?? null
          try {
            const result =
              kind === 'tutor-retry'
                ? await api.retryAgentChat(
                    classId,
                    turnSessionId,
                    undefined,
                    controller.signal,
                    onAgentEvent,
                  )
                : kind === 'regenerate'
                  ? await api.regenerateAgentChat(
                      classId,
                      turnSessionId,
                      // A manual regeneration uses the CURRENT selection, like the tutor's;
                      // a body-less one (the just-in-time continuation) keeps the stored scope.
                      { mode: activeMode, documentId: agentDocumentId },
                      controller.signal,
                      onAgentEvent,
                    )
                  : await api.sendAgentChat(
                      classId,
                      turnSessionId,
                      content,
                      undefined,
                      agentDocumentId,
                      activeMode,
                      // PLA-313: one operation ID per logical Send. It is minted by `send`
                      // and retained across ambiguous failures so a resubmit reconciles
                      // instead of duplicating.
                      operationIdRef.current ?? undefined,
                      controller.signal,
                      onAgentEvent,
                    )
            if (!owns()) return
            if (result.stopped === 'stopped') {
              // The server settled this turn as STOPPED - our Stop, or the server's own
              // backstop - and completed the request with its bounded stopped body.
              // There is no reply to show: present the terminal stopped state (not a
              // completion of an empty answer) and spend the send key, exactly as a
              // confirmed Stop would.
              if (outcomeRef.current === 'active') setOutcome('stopped')
              if (kind === 'send') {
                responseAcceptedRef.current = true
                submittedTextRef.current = null
                operationIdRef.current = null
                if (streamTextRef.current.trim().length === 0) {
                  revealDrainedRef.current = true
                  setRevealDrained(true)
                }
              }
            } else {
              // Replayed operations and older servers return a complete JSON answer.
              // Keep it in the optimistic renderer until its reveal has drained too.
              if (owns() && result.content !== assistantText) {
                assistantText = result.content
                streamTextRef.current = assistantText
                setStreamText(assistantText)
              }
              onEvent({ type: 'done', message_id: result.message_id })
              if (kind === 'send') {
                // The operation is durably settled: the reply committed (or the failure is
                // durable), so the key is spent. A retry or regeneration must never clear a
                // key an earlier ambiguous send still owns.
                responseAcceptedRef.current = true
                submittedTextRef.current = null
                operationIdRef.current = null
              }
            }
          } catch (err) {
            // The turn's truth is durable on the server either way: a failed attempt row, or
            // a committed reply whose acceptance the transport lost. Refresh the transcript
            // and the work surface before surfacing the error, so the conversation shows the
            // failed turn or the recovered answer instead of a stale cache.
            await invalidateAgentTurnCaches(queryClient, classId, turnSessionId).catch(
              () => undefined,
            )
            throw err
          }
          await invalidateAgentTurnCaches(queryClient, classId, turnSessionId).catch(
            () => undefined,
          )
          return
        }
        const documentId = scopedDocument?.id ?? null
        await (kind === 'writer-retry' && writer
          ? streamWriterChatRetry(writer.artifactId, turnSessionId, onEvent, controller.signal)
          : writer
            ? streamWriterChat(
                writer.artifactId,
                turnSessionId,
                { content },
                onEvent,
                controller.signal,
              )
            : kind === 'tutor-retry'
              ? streamChatRetry(
                  turnSessionId,
                  { mode: activeMode, document_id: documentId },
                  onEvent,
                  controller.signal,
                )
              : kind === 'regenerate'
                ? streamRegenerate(
                    turnSessionId,
                    { mode: activeMode, document_id: documentId },
                    onEvent,
                    controller.signal,
                  )
                : streamChat(
                    turnSessionId,
                    {
                      content,
                      mode: activeMode,
                      document_id: documentId,
                      operation_id: operationIdRef.current ?? undefined,
                    },
                    onEvent,
                    controller.signal,
                    () => {
                      if (!owns()) return
                      responseAcceptedRef.current = true
                      submittedTextRef.current = null
                      operationIdRef.current = null
                    },
                  ))
      } catch (caught) {
        if (!owns()) {
          // Nothing to report and nobody to report it to: this turn's rows are gone.
        } else if (caught instanceof DOMException && caught.name === 'AbortError') {
          if (outcomeRef.current === 'active') {
            setOutcome('stopped')
            if (submittedTextRef.current !== null) {
              restoreDraft?.()
              submittedTextRef.current = null
            }
            if (streamTextRef.current.trim().length === 0) {
              revealDrainedRef.current = true
              setRevealDrained(true)
            }
          }
        } else if (caught instanceof ApiError && caught.status === 409) {
          if (caught.code === 'writer_retry_has_effects') {
            toast.error(
              'The previous attempt made changes before it failed. Review what landed, then send a new message.',
            )
          } else if (caught.code === 'operation_id_mismatch') {
            toast.error(caught.detail)
          } else {
            toast.error('Another turn is still in progress on this conversation.')
          }
          if (kind === 'send') {
            restoreDraft?.()
            // PLA-313: a busy 409 means THIS attempt was not accepted; it says nothing
            // about whether the earlier ambiguous operation committed. Keep the
            // ambiguity key (operationIdRef) and its submitted-text bookmark so a later
            // identical Send carries the same operation ID and the backend reconciles
            // or replays it instead of creating a second durable question. Only a true
            // operation-ID mismatch - a structured 409 proving the key itself is spent
            // on a different request - may discard it so the next Send mints fresh.
            if (caught.code === 'operation_id_mismatch') {
              operationIdRef.current = null
              submittedTextRef.current = null
            }
          }
          // A non-send turn kind never carries the ambiguity key, so a 409 on it must
          // not clear one either: an earlier ambiguous send's X stays intact.
          setOutcome('failed')
        } else {
          setOutcome('failed')
          if (agent && kind === 'send') {
            // Lost-response reconciliation keyed on the logical send's OPERATION ID
            // (PLA-313), never on message text: this conversation may already contain an
            // identical earlier question with its own completed turn, and text equality
            // would let that old turn satisfy the new operation - clearing the composer
            // and silently losing the new question. The durable readback carries the
            // operation id on the attempt of the user message the send committed to; a
            // message that carries THIS id is the send's own turn, in whatever state it
            // settled in. The screen above already fell back to the durable state, so
            // every branch shows the truth.
            const durable = await api.listMessages(turnSessionId).catch(() => null)
            if (!owns()) return
            const operationId = operationIdRef.current
            const ownTurn =
              durable === null || operationId === null
                ? undefined
                : durable.find(
                    (item) =>
                      item.role === 'user' && item.agent_attempt?.operation_id === operationId,
                  )
            if (ownTurn !== undefined) {
              // The send committed durably - completed, failed, stopped, or still
              // settling. It now has a durable identity of its own, so the browser's
              // send key is spent: retire BOTH refs, whatever the attempt's state.
              // (A failed or stopped turn keeps its causal Retry; a subsequent Send -
              // identical or different - is a NEW send with a fresh operation ID, never
              // a re-run of the spent key.)
              if (ownTurn.agent_attempt?.state === 'completed') {
                // The turn succeeded and only its acceptance was lost: the reply is in
                // the transcript; keep any follow-up draft and mint a fresh operation
                // ID on the next Send.
                submittedTextRef.current = null
                operationIdRef.current = null
                responseAcceptedRef.current = true
                toast.success('Your answer was already saved.')
              } else {
                // Durable failed/stopped (or still settling) turn, no usable reply: the
                // transcript shows the honest turn with its Retry. Keep any follow-up
                // draft and retire the spent key.
                submittedTextRef.current = null
                operationIdRef.current = null
                toast.error(
                  caught instanceof ApiError ? caught.message : 'The answer stopped early.',
                )
              }
            } else {
              // Nothing durable carries this operation: the request never reached the
              // server (or the readback failed). The draft goes back in the box and the
              // minted key is kept, so a re-send either lands fresh or - if the turn did
              // commit and only the readback missed it - the server reconciles by that
              // same id instead of duplicating.
              if (submittedTextRef.current !== null) {
                restoreDraft?.()
              }
              toast.error(caught instanceof ApiError ? caught.message : 'The answer stopped early.')
            }
          } else {
            // Restore an unaccepted question only if no newer draft owns the box.
            toast.error(caught instanceof ApiError ? caught.message : 'The answer stopped early.')
            if (submittedTextRef.current !== null) {
              restoreDraft?.()
            }
          }
        }
      } finally {
        if (owns()) {
          abortRef.current = null
          // The turn is gone: any in-flight Stop belongs to it, so its "Stopping…"
          // state ends with the turn no matter how the turn settled.
          stopInFlightRef.current = false
          setStopping(false)
          if (outcomeRef.current === 'active') {
            toast.error('The answer stopped early.')
            setOutcome('failed')
          }
        } else {
          // An answer written while the student was reading something else. Marking its
          // transcript stale is the whole of how it reaches them: there were no optimistic
          // rows to settle, so opening that conversation is what fetches what it said.
          void queryClient.invalidateQueries({ queryKey: chatKeys.messages(turnSessionId) })
        }
      }
    },
    [
      agent,
      activeMode,
      classId,
      detached,
      pendingTurn,
      persisted,
      placeholderReply,
      queryClient,
      scopedDocument,
      setOutcome,
      writer,
    ],
  )

  const send = useCallback(
    (content: string) => {
      const trimmed = content.trim()
      if (
        sendingRef.current !== null ||
        trimmed.length === 0 ||
        showingTurn ||
        historyError ||
        messagesPending ||
        (sessionListNeeded && sessionsPending)
      )
        return
      if (
        operationIdRef.current !== null &&
        submittedTextRef.current !== null &&
        trimmed !== submittedTextRef.current
      ) {
        operationIdRef.current = null
      }
      expectedHandoffRef.current = null
      const sendId = Symbol()
      sendingRef.current = sendId
      const scopeVersion = draftScopeVersionRef.current
      const ownsSend = () => sendingRef.current === sendId
      setSending(true)
      const revision = draftRevisionRef.current
      const restoreDraft = () => {
        if (draftRevisionRef.current === revision) setDraft(content)
      }
      operationIdRef.current ??= crypto.randomUUID()
      responseAcceptedRef.current = false
      submittedTextRef.current = trimmed
      setDraft('')
      void (async () => {
        const target = await ensureSession(ownsSend)
        if (!ownsSend() || draftScopeVersionRef.current !== scopeVersion) return
        // The question goes back in the box rather than into the void: the conversation
        // could not be opened, so there is nowhere for it to have gone.
        if (target === null) {
          restoreDraft()
          submittedTextRef.current = null
          return
        }
        await runTurn('send', trimmed, target, restoreDraft)
      })().finally(() => {
        if (ownsSend()) {
          sendingRef.current = null
          setSending(false)
        }
      })
    },
    [
      ensureSession,
      runTurn,
      historyError,
      messagesPending,
      sessionListNeeded,
      sessionsPending,
      showingTurn,
    ],
  )

  /**
   * A question typed on another surface and handed off with send set: it goes the moment
   * the pane can send it. Waits for settings to load so a missing endpoint leaves the
   * words in the composer with the reason visible, rather than firing into a composer
   * that cannot answer. Fires once: the page strips the handoff from the URL, and this
   * ref covers the renders in between.
   */
  const autoSentRef = useRef(false)
  useEffect(() => {
    if (!initialSend || !initialAsk || autoSentRef.current) return
    if (
      settings === undefined ||
      historyError ||
      messagesPending ||
      (sessionListNeeded && sessionsPending)
    )
      return
    autoSentRef.current = true
    if (disabledReason) return
    // Deferred a tick so the send is not a state change inside the effect body itself;
    // the ref above already guarantees exactly one schedule, so there is no cleanup to
    // race against.
    window.setTimeout(() => send(initialAsk), 0)
  }, [
    disabledReason,
    initialAsk,
    initialSend,
    send,
    settings,
    historyError,
    messagesPending,
    sessionListNeeded,
    sessionsPending,
  ])

  /**
   * Retry means answer again, not ask again. The question stays put and its reply is
   * replaced, which is the only reading of the button that makes sense: a student presses
   * it because the answer was wrong, not because they forgot they had asked.
   */
  const regenerate = useCallback(() => {
    if (activeSessionId === null || writer) return
    void runTurn('regenerate', '', activeSessionId)
  }, [activeSessionId, runTurn, writer])

  const retryTutorTurn = useCallback(() => {
    if (activeSessionId === null || writer) return
    void runTurn('tutor-retry', '', activeSessionId)
  }, [activeSessionId, runTurn, writer])

  const retryWriterTurn = useCallback(() => {
    if (activeSessionId === null || !writer) return
    void runTurn('writer-retry', '', activeSessionId)
  }, [activeSessionId, runTurn, writer])

  /**
   * A settling Stop's wait for the backend's proof that the session is free: a calm,
   * bounded poll of the status endpoint. "Stopping…" and the closed conversation last
   * for the whole stretch - the turn is stopped in every way that matters (no reply will
   * arrive, no further durable effect can land), but the conversation must not re-enable
   * until the backend proves the stopped turn's release has finished: every worker has
   * provably left, the attempt is durably settled, and the session's claim is free.
   * Returns true when that proof arrives; false when the bound elapses first or the pane
   * is no longer owned by this turn (the caller then leaves Stop available to try again).
   */
  const pollStopStatus = useCallback(
    async (classId: number, session: number, owner: number): Promise<boolean> => {
      const deadline = performance.now() + _STOP_SETTLE_LIMIT_MS
      while (turnIdRef.current === owner) {
        let settling = true
        try {
          settling = (await api.stopAgentChatStatus(classId, session)).settling
        } catch {
          // A dropped status read is not proof of anything: the session is merely still
          // settling. Wait a beat and ask again.
        }
        if (turnIdRef.current !== owner) return false
        if (!settling) return true
        if (performance.now() >= deadline) return false
        await new Promise((resolve) => {
          window.setTimeout(resolve, _STOP_SETTLE_POLL_MS)
        })
      }
      return false
    },
    [],
  )

  const stop = useCallback(() => {
    const pending = abortRef.current
    if (pending === null) return
    const session = turnSessionRef.current
    if (agentTurnRef.current === false || session === null) {
      // Non-agent turns keep their longstanding behavior: the stream belongs to this
      // connection, so the local abort IS the whole stop.
      if (outcomeRef.current === 'active') setOutcome('stopped')
      if (responseAcceptedRef.current) {
        operationIdRef.current = null
      }
      if (streamTextRef.current.trim().length === 0) {
        revealDrainedRef.current = true
        setRevealDrained(true)
      }
      pending.abort()
      return
    }
    // The agent's turn is non-streaming: its server handler cannot see this fetch being
    // aborted, so the explicit /stop is what actually stops the work. The UI enters a
    // bounded "Stopping…" state and AWAITs the server's verdict: the student must not be
    // told "stopped" before the server has proved it, and the conversation stays closed
    // to another turn meanwhile.
    if (stopInFlightRef.current) return
    stopInFlightRef.current = true
    setStopping(true)
    // Identity of the turn this stop belongs to: the turn counter only moves when a
    // new turn starts, so a stop callback arriving after a newer turn owns the pane
    // must not touch the newer turn's state.
    const owner = turnIdRef.current
    // The terminal action, applied exactly once the server has proved the stop: mark the
    // turn stopped (if still active), spend the send key, drain the reveal queue, and
    // stand down the local request. Idempotent - the turn's own settled response may
    // land in the same instant with exactly these effects.
    const confirmStopped = () => {
      if (turnIdRef.current !== owner) return
      if (outcomeRef.current === 'active') setOutcome('stopped')
      responseAcceptedRef.current = true
      submittedTextRef.current = null
      operationIdRef.current = null
      if (streamTextRef.current.trim().length === 0) {
        revealDrainedRef.current = true
        setRevealDrained(true)
      }
      pending.abort()
    }
    void (async () => {
      try {
        const verdict = await api.stopAgentChat(classId, session)
        if (turnIdRef.current !== owner) return
        if (verdict.stopped) {
          // Settled and quiescent: the session is free and the turn's work is done.
          confirmStopped()
        } else if (verdict.settling) {
          // The stop was latched but a worker is still inside a dispatch: the turn is
          // stopped in every way that matters, but the conversation stays closed -
          // "Stopping…", not sendable - until the backend proves the session free.
          const provenFree = await pollStopStatus(classId, session, owner)
          if (provenFree) {
            confirmStopped()
          } else if (turnIdRef.current === owner && outcomeRef.current === 'active') {
            // The bound elapsed without the proof: the stop was latched, but this pane
            // will not claim a stop that was not proven. The Stop affordance comes back
            // (the finally) so the student can try again.
            toast.error('Stop could not be confirmed. The turn may still be running.')
          }
        }
        // Else: nothing was in flight when /stop inspected it - the turn settled in the
        // race just before the Stop (completed, or failed). This Stop changed nothing
        // durable: the turn's own request (or its lost-response reconciliation) settles
        // the outcome, and nothing here may label it stopped or spend the send key.
      } catch {
        // The stop did not reach the server (or the server could not handle it): the turn
        // may still be running. Stay active - do NOT present a stop that was never
        // confirmed - and let the student try Stop again.
        if (turnIdRef.current === owner && outcomeRef.current === 'active') {
          toast.error('Stop could not be confirmed. The turn may still be running.')
        }
      } finally {
        if (turnIdRef.current === owner) {
          stopInFlightRef.current = false
          setStopping(false)
        }
      }
    })()
  }, [classId, pollStopStatus])

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

  // Detached, the turn is somewhere else's: the composer here offers Send rather than the
  // Stop belonging to an answer this conversation is not the one waiting on.
  const optimisticTurn = showingTurn && turnOutcome !== 'failed'
  const turnActive = showingTurn && turnOutcome === 'active'
  const rendered = optimisticTurn
    ? messages.map((message) =>
        message.id === -2 ? { ...message, content: streamText, thinking: streamThinking } : message,
      )
    : messages
  const lastAssistantIndex = rendered.reduce(
    (found, message, index) => (message.role === 'assistant' ? index : found),
    -1,
  )
  const lastUserIndex = rendered.reduce(
    (found, message, index) => (message.role === 'user' ? index : found),
    -1,
  )

  // A conversation opens at its latest message, and follows the stream while the reader
  // is already at the tail. Scrolling up to re-read detaches the follow, and the jump
  // button is the way back.
  // Inline, the conversation does not own a scroll container: it flows into the pane it
  // was opened inside, and follows the stream there. Everything below reads `viewportRef`,
  // so the follow, the jump button, and the re-pin work the same either way. One real ref
  // resolved once per render rather than a ternary at each use: a conditional ref is not
  // one the compiler can see through, and half of this file depends on it being stable.
  const ownViewportRef = useRef<HTMLDivElement>(null)
  const viewportRef = useRef<HTMLDivElement | null>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const [following, setFollowing] = useState(true)
  const followingRef = useRef(true)
  const userScrollAtRef = useRef(0)

  useEffect(() => {
    viewportRef.current = inline ? (scrollViewportRef?.current ?? null) : ownViewportRef.current
  })

  /** How far the tail of the conversation sits below the bottom of the viewport. */
  const distanceBelowFold = useCallback((): number => {
    const node = viewportRef.current
    if (!node) return 0
    // Inline, the tail is the end of this thread, not the end of the pane: the pane
    // continues with the next problem, and scrolling to its bottom would carry the reader
    // straight past the answer they are waiting on.
    const content = inline ? contentRef.current : null
    if (content) {
      return content.getBoundingClientRect().bottom - node.getBoundingClientRect().bottom
    }
    return node.scrollHeight - node.scrollTop - node.clientHeight
  }, [inline])

  const scrollToBottom = useCallback(
    (behavior: ScrollBehavior) => {
      const node = viewportRef.current
      if (!node) return
      const distance = distanceBelowFold()
      // Nothing below the fold is nothing to follow. Inline, a thread that already fits on
      // screen has a negative distance, and "scrolling to the bottom" of it would drag the
      // pane *upward* to sit its last line on the last pixel. That fired on every reasoning
      // token, which is why the pane could not be scrolled at all while Lyra was thinking
      // and came free the moment the answer grew past the fold.
      if (distance <= 0) return
      node.scrollTo({ top: Math.max(0, node.scrollTop + distance), behavior })
    },
    [distanceBelowFold],
  )

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
      const atBottom = distanceBelowFold() <= STICK_THRESHOLD_PX
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
  }, [distanceBelowFold])

  // Opening a conversation jumps straight to the end, with no visible travel through
  // history the reader did not ask to see. Not inline: the reader opened this thread from
  // a step they were reading, and moving the pane under them would take that step away.
  useLayoutEffect(() => {
    if (messagesPending || inline) return
    followingRef.current = true
    scrollToBottom('instant')
  }, [activeSessionId, inline, messagesPending, scrollToBottom])

  // Sending a question, or asking for the answer again, is an unambiguous request to be at
  // the tail: the reply lands at the bottom and the reader is now waiting on it. This is
  // the one place following re-attaches without the reader scrolling there, and it is
  // correct precisely because they just acted. `turnStartedAt` is a fresh timestamp per
  // turn, so this fires once for each.
  useEffect(() => {
    if (turnStartedAt === null) return
    followingRef.current = true
    // After paint, so the scroll measures the height the new rows actually occupy.
    const frame = requestAnimationFrame(() => {
      setFollowing(true)
      scrollToBottom('smooth')
    })
    return () => cancelAnimationFrame(frame)
  }, [turnStartedAt, scrollToBottom])

  // A new message animates into view; streamed tokens do not, because a smooth scroll
  // restarting on every token never arrives.
  useEffect(() => {
    if (!followingRef.current) return
    scrollToBottom('smooth')
  }, [rendered.length, scrollToBottom])

  useEffect(() => {
    if (!followingRef.current || !optimisticTurn) return
    scrollToBottom('instant')
  }, [streamText, streamThinking, processingStage, optimisticTurn, scrollToBottom])

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

  // A segmented control rather than underlined tabs. These do not navigate anywhere - they
  // change how the next answer is written. In the header bar there is no pane rule for an
  // underline to sit on, so the honest idiom is a switch with a travelling thumb.
  // The writer has no pedagogy modes - there is one writer. The contextual agent keeps
  // them: the mode still governs how the work is presented, and the shared mode contract
  // inherits it (the agent plans tools, it does not own the pedagogy).
  const modeToggle = writer ? null : (
    <div
      className="border-border/70 bg-muted/70 flex items-center rounded-full border p-0.5"
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
                'h-7 rounded-full px-3 text-xs transition-colors duration-150',
                activeMode === option.value
                  ? 'bg-card text-foreground hover:bg-card shadow-sm'
                  : 'text-text-secondary hover:bg-transparent hover:text-foreground',
              )}
              onClick={() => {
                if (!inline && sessionId === null && activeSessionId !== null) {
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
  )

  const paneControls = (
    <div className="flex items-center gap-1.5">
      {modeToggle}
      {headerActions}
    </div>
  )

  const conversation = (
    <div
      ref={contentRef}
      className={cn(inline ? 'space-y-5' : 'mx-auto max-w-[860px] p-4 md:px-6')}
    >
      {historyError ? (
        <div role="alert" className="mb-4 rounded-md border border-danger-border p-3 text-sm">
          <p>Could not load this conversation.</p>
          <p className="text-text-secondary">Retry to load saved messages before continuing.</p>
          <Button
            variant="outline"
            size="sm"
            className="mt-2"
            onClick={() => {
              if (sessionListNeeded && sessionsError) void retrySessions()
              if (activeSessionId !== null && messagesError) void retryMessages()
            }}
          >
            Retry conversation
          </Button>
        </div>
      ) : null}
      {/* The writer never falls back to the class's newest session, so its empty state
          must not wait on the class session list either. */}
      {(sessionListNeeded && sessionsPending) || messagesPending ? (
        <div className="space-y-4" aria-busy="true" aria-label="Loading conversation">
          <Skeleton className="ml-auto h-12 w-2/3" />
          <Skeleton className="h-24 w-full" />
        </div>
      ) : historyError && rendered.length === 0 ? null : rendered.length === 0 &&
        emptyState !== undefined ? (
        emptyState
      ) : rendered.length === 0 ? (
        <EmptyConversation
          className={className}
          readyCount={readyCount}
          hasProfile={(profile?.facts.length ?? 0) > 0}
          suggestions={suggestions}
          onPick={changeDraft}
        />
      ) : (
        rendered.map((message, index) => {
          const isStreamingReply = optimisticTurn && message.id === -2
          return (
            <MessageRow
              key={message.id}
              message={message}
              // A question and the answer under it are one turn, so they sit close; the
              // next question opens at a wider interval. Even spacing throughout is what
              // made a transcript read as an undifferentiated stack of blocks.
              className={cn(!inline && index > 0 && (message.role === 'user' ? 'mt-11' : 'mt-5'))}
              startsTimeGap={startsTimeGap(rendered, index)}
              streaming={isStreamingReply}
              activity={isStreamingReply ? streamActivity : undefined}
              processingStage={isStreamingReply ? processingStage : null}
              turnStartedAt={isStreamingReply ? turnStartedAt : null}
              turnEnded={
                isStreamingReply
                  ? turnOutcome === 'completed' || turnOutcome === 'stopped'
                  : undefined
              }
              onRevealComplete={isStreamingReply ? handleRevealComplete : undefined}
              canRetry={
                !historyError &&
                !messagesPending &&
                !optimisticTurn &&
                index === lastAssistantIndex &&
                !writer
              }
              onRetry={
                historyError || messagesPending
                  ? undefined
                  : writer
                    ? !optimisticTurn &&
                      index === lastUserIndex &&
                      (message.writer_attempt?.state === 'failed' ||
                        message.writer_attempt?.state === 'stopped')
                      ? retryWriterTurn
                      : undefined
                    : !optimisticTurn &&
                        index === lastUserIndex &&
                        (message.agent_attempt?.state === 'failed' ||
                          message.agent_attempt?.state === 'stopped' ||
                          message.tutor_attempt?.state === 'failed' ||
                          message.tutor_attempt?.state === 'stopped')
                      ? retryTutorTurn
                      : message.role === 'assistant'
                        ? regenerate
                        : undefined
              }
            />
          )
        })
      )}
    </div>
  )

  const composer = (
    <Composer
      value={draft}
      onChange={changeDraft}
      onSend={() => send(draft)}
      onStop={stop}
      streaming={turnActive}
      stopping={stopping}
      disabledReason={disabledReason}
      sendBlocked={showingTurn || sending}
      blocked={historyError || messagesPending || (sessionListNeeded && sessionsPending)}
      sourceControl={sourceControl}
      workspaceControl={workspaceControl}
      // Inline, the reader clicked to open this and the next thing they do is type.
      autoFocus={inline || layout === 'assistant'}
    />
  )

  if (inline) {
    // No scroll container of its own and no header rule: this is a passage of the page it
    // was opened inside, not a panel sitting on top of one. The writer variant usually
    // has neither a toggle nor actions, and an empty control row would be a blank gap.
    return (
      <div className="flex flex-col gap-4">
        {modeToggle || headerActions ? (
          <div className="flex items-center gap-2">
            {modeToggle}
            {headerActions ? (
              <div className="ml-auto flex shrink-0 items-center gap-1">{headerActions}</div>
            ) : null}
          </div>
        ) : null}
        {conversation}
        {composer}
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* On a wide screen these go into the header bar the route already has, rather than a
          second full-width rule directly beneath it: the old bar cost 40px to say "TUTOR",
          a word the breadcrumb above it and the composer below it both already imply.
          Narrow, that header is already holding a breadcrumb, Profile, and the endpoint
          badge in 375px, so the controls stay in the pane instead of crushing it. */}
      {layout === 'assistant' ? null : wide ? (
        <HeaderActions>{paneControls}</HeaderActions>
      ) : (
        <div className="flex shrink-0 items-center gap-2 px-4 pt-3">{paneControls}</div>
      )}

      <div className="relative min-h-0 flex-1">
        <ScrollArea viewportRef={ownViewportRef} className="h-full">
          {conversation}
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

      {/* No rule above the well: the composer is a raised object standing on the pane, and
          it carries its own edge. What separates them is a scrim instead — the conversation
          dissolves into the canvas over the last few millimetres rather than being sliced
          off mid-line by a hard edge. Functional, not ornament: it is the only thing saying
          the text continues above. */}
      <div className="relative shrink-0 p-4 pt-2 md:px-6">
        <div
          aria-hidden
          className="from-background/0 to-background pointer-events-none absolute inset-x-0 -top-10 h-10 bg-gradient-to-b"
        />
        <div className="relative mx-auto max-w-[860px]">{composer}</div>
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
    // A title page, not a dashboard: centered, set in the display face, opened by the
    // house fleuron. The suggestions read as a contents list rather than a button pile.
    <div className="mx-auto flex min-h-[60vh] w-full max-w-xl flex-col items-center justify-center py-8 text-center">
      <div className="text-accent-primary mb-5 size-9" aria-hidden>
        <LyraMark />
      </div>
      <h2 className="font-display text-[2rem] leading-tight text-balance">{className}</h2>
      <p className="text-text-secondary mt-2 text-sm">
        {readyCount === 0
          ? 'Nothing indexed yet. Upload a document and Lyra will have something to work from.'
          : `${formatCount(readyCount, 'document')} indexed${hasProfile ? ', syllabus analyzed' : ''}.`}
      </p>
      <Asterism className="text-border-strong mt-8 mb-6" />
      <p className="eyebrow mb-1">Try asking</p>
      <div className="flex w-full flex-col items-stretch">
        {suggestions.map((prompt) => (
          <button
            key={prompt}
            type="button"
            onClick={() => onPick(prompt)}
            className="group/prompt border-border/70 text-text-secondary hover:text-text-primary focus-visible:ring-ring flex items-baseline justify-between gap-3 border-b py-3 text-left text-sm transition-colors duration-150 last:border-b-0 focus-visible:ring-2 focus-visible:outline-none"
          >
            <span className="min-w-0">{prompt}</span>
            <span
              aria-hidden
              className="text-accent-primary shrink-0 translate-x-0 opacity-0 transition-[opacity,transform] duration-150 group-hover/prompt:translate-x-0.5 group-hover/prompt:opacity-100 group-focus-visible/prompt:opacity-100"
            >
              →
            </span>
          </button>
        ))}
      </div>
    </div>
  )
}
