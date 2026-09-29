'use client'

import { useEffect, useId, useRef, useState, useSyncExternalStore } from 'react'
import { ArrowUp } from 'lucide-react'

import { Asterism } from '@/components/ui/asterism'
import {
  clearChatDraftIfRevision,
  beginChatDraftSend,
  finishChatDraftSend,
  getChatDraftSettlementOutcome,
  getChatDraftSettlementVersion,
  hasChatDraftAccepted,
  hasChatDraftRetirementRisk,
  isChatDraftSendPending,
  readChatDraft,
  subscribeChatDraftSends,
  subscribeChatDraftSettlements,
  writeChatDraft,
} from '@/lib/chat-draft-store'
import { useMediaQuery } from '@/lib/hooks/use-media-query'

const GENERAL_PROMPTS = [
  'Help me make sense of a difficult concept…',
  'Walk me through a problem, one step at a time…',
  'Help me find a starting point for my writing…',
]

/** A blank sheet, with ideas that yield as soon as the student starts writing.
 *
 * Questions survive navigation and relaunch until the exact submitted revision is accepted.
 */
export function ClassAskComposer({
  className,
  draftKey,
  suggestions = GENERAL_PROMPTS,
  onSend,
}: {
  className?: string
  draftKey: string
  suggestions?: string[]
  onSend: (question: string) => void | Promise<void>
}) {
  const scope = `class-ask:${draftKey}`
  const revision = useRef<string | null>(null)
  const acceptedOnRestore = useRef(false)
  const [question, setQuestion] = useState(() => {
    const saved = readChatDraft(scope)
    acceptedOnRestore.current = hasChatDraftAccepted(scope)
    if (saved) {
      revision.current = saved.revision
      return saved.value
    }
    if (acceptedOnRestore.current) return ''
    try {
      return sessionStorage.getItem(draftKey) ?? ''
    } catch {
      return ''
    }
  })
  const [index, setIndex] = useState(0)
  const [focused, setFocused] = useState(false)
  const [sending, setSending] = useState(false)
  const pendingInWindow = useSyncExternalStore(
    subscribeChatDraftSends,
    () => isChatDraftSendPending(scope),
    () => false,
  )
  const settlementVersion = useSyncExternalStore(
    subscribeChatDraftSettlements,
    () => getChatDraftSettlementVersion(scope),
    () => 0,
  )
  const [error, setError] = useState(false)
  const [storageWarning, setStorageWarning] = useState(false)
  const [settlementWarning, setSettlementWarning] = useState(() =>
    hasChatDraftRetirementRisk(scope),
  )
  const submitting = useRef(false)
  const textarea = useRef<HTMLTextAreaElement>(null)
  const reduceMotion = useMediaQuery('(prefers-reduced-motion: reduce)')
  const id = useId()
  const prompts = suggestions.length ? suggestions : GENERAL_PROMPTS
  const prompt = prompts[index % prompts.length]
  const empty = question.length === 0

  useEffect(() => {
    if (revision.current !== null || !question) return
    const saved = writeChatDraft(scope, question)
    revision.current = saved.record.revision
    setStorageWarning(!saved.durable)
  }, [question, scope])

  useEffect(() => {
    if (pendingInWindow || !revision.current) return
    const current = readChatDraft(scope)
    if (current?.revision !== revision.current) {
      revision.current = current?.revision ?? null
      setQuestion(current?.value ?? '')
    }
    setSettlementWarning(
      getChatDraftSettlementOutcome(scope) === 'failed' || hasChatDraftRetirementRisk(scope),
    )
  }, [pendingInWindow, scope, settlementVersion])

  function changeQuestion(value: string) {
    setQuestion(value)
    const saved = writeChatDraft(scope, value)
    revision.current = saved.record.revision
    setStorageWarning(!saved.durable)
    setSettlementWarning(hasChatDraftRetirementRisk(scope))
  }

  useEffect(() => {
    if (!empty || focused || reduceMotion || sending || prompts.length < 2) return
    let timer: ReturnType<typeof setInterval> | undefined
    const sync = () => {
      clearInterval(timer)
      if (!document.hidden) timer = setInterval(() => setIndex((value) => value + 1), 6000)
    }
    sync()
    document.addEventListener('visibilitychange', sync)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', sync)
    }
  }, [empty, focused, reduceMotion, sending, prompts.length])

  async function send() {
    if (!question.trim() || submitting.current || pendingInWindow) return
    const sendToken = beginChatDraftSend(scope)
    if (sendToken === null) return
    submitting.current = true
    const submittedRevision = revision.current
    setSending(true)
    setError(false)
    try {
      await onSend(question.trim())
      const outcome = submittedRevision
        ? clearChatDraftIfRevision(scope, submittedRevision)
        : 'retired'
      setSettlementWarning(outcome === 'failed' || hasChatDraftRetirementRisk(scope))
      if (outcome !== 'failed') setStorageWarning(false)
      if (outcome !== 'failed') {
        try {
          sessionStorage.removeItem(draftKey)
        } catch {
          // The durable record has already been settled.
        }
      }
      if (revision.current === submittedRevision) {
        revision.current = null
        setQuestion('')
      }
    } catch {
      setError(true)
      textarea.current?.focus()
    } finally {
      finishChatDraftSend(sendToken)
      submitting.current = false
      setSending(false)
    }
  }

  return (
    <section aria-label="Ask Lyra">
      <form
        className="class-ask-sheet"
        aria-busy={sending}
        onSubmit={(event) => {
          event.preventDefault()
          void send()
        }}
      >
        <div className="class-ask-heading">
          <label htmlFor={id} className="class-ask-title">
            What shall we explore?
          </label>
          <Asterism className="class-ask-ornament" />
        </div>
        <div className="class-ask-writing">
          {empty ? (
            <span id={`${id}-idea`} key={prompt} className="class-ask-prompt">
              {prompt}
            </span>
          ) : null}
          <textarea
            ref={textarea}
            id={id}
            name="question"
            rows={4}
            value={question}
            readOnly={sending}
            aria-label={`Ask about ${className ?? 'this class'}`}
            aria-describedby={empty ? `${id}-idea` : undefined}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            onChange={(event) => changeQuestion(event.target.value)}
            onKeyDown={(event) => {
              if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                void send()
              }
            }}
          />
        </div>
        <div className="class-ask-footer">
          <button
            type="submit"
            disabled={!question.trim() || sending || pendingInWindow}
            className="class-ask-send"
            aria-label="Ask"
            title={sending ? 'Opening conversation' : 'Send message'}
          >
            <ArrowUp aria-hidden="true" />
          </button>
        </div>
        {error ? (
          <p role="alert" className="mt-3 text-sm text-danger-text">
            Could not open the conversation. Your question is still here; try again.
          </p>
        ) : null}
        {storageWarning ? (
          <p role="status" className="mt-3 text-sm text-danger-text">
            This question is only in this window. Make a copy before closing Lyra.
          </p>
        ) : null}
        {settlementWarning ? (
          <p role="alert" className="mt-3 text-sm text-danger-text">
            Your question was sent, but its saved copy could not be removed. It may reappear after
            restart; do not send it again.
          </p>
        ) : null}
      </form>
    </section>
  )
}
