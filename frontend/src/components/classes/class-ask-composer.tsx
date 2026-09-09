'use client'

import { useEffect, useId, useRef, useState } from 'react'
import { ArrowUp, Pause, Play } from 'lucide-react'

import { Asterism } from '@/components/ui/asterism'
import { Button } from '@/components/ui/button'
import { useMediaQuery } from '@/lib/hooks/use-media-query'

const GENERAL_PROMPTS = [
  'Help me make sense of a difficult concept…',
  'Walk me through a problem, one step at a time…',
  'Help me find a starting point for my writing…',
]

/** A blank sheet, with ideas that yield as soon as the student starts writing. */
export function ClassAskComposer({
  className,
  suggestions = GENERAL_PROMPTS,
  onSend,
}: {
  className?: string
  suggestions?: string[]
  onSend: (question: string) => void | Promise<void>
}) {
  const [question, setQuestion] = useState('')
  const [index, setIndex] = useState(0)
  const [focused, setFocused] = useState(false)
  const [paused, setPaused] = useState(false)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState(false)
  const submitting = useRef(false)
  const textarea = useRef<HTMLTextAreaElement>(null)
  const reduceMotion = useMediaQuery('(prefers-reduced-motion: reduce)')
  const id = useId()
  const prompts = suggestions.length ? suggestions : GENERAL_PROMPTS
  const prompt = prompts[index % prompts.length]
  const empty = question.length === 0

  useEffect(() => {
    if (!empty || focused || paused || reduceMotion || sending || prompts.length < 2) return
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
  }, [empty, focused, paused, reduceMotion, sending, prompts.length])

  async function send() {
    if (!question.trim() || submitting.current) return
    submitting.current = true
    setSending(true)
    setError(false)
    try {
      await onSend(question.trim())
    } catch {
      setError(true)
      textarea.current?.focus()
    } finally {
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
        <div className="class-ask-inscription" aria-hidden="true">
          <span>A little curiosity goes a long way</span>
          <Asterism className="text-gold shrink-0" />
        </div>
        <label htmlFor={id} className="class-ask-title">
          What shall we explore?
        </label>
        <div className="class-ask-writing">
          {empty ? (
            <span key={prompt} className="class-ask-prompt" aria-hidden="true">
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
            aria-describedby={`${id}-hint`}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            onChange={(event) => setQuestion(event.target.value)}
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
          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
            {empty ? (
              <button
                type="button"
                className="class-ask-use-prompt"
                aria-label={`Use this idea: ${prompt}`}
                onClick={() => {
                  setQuestion(prompt)
                  textarea.current?.focus()
                }}
              >
                Use this idea <span aria-hidden="true">↗</span>
              </button>
            ) : null}
            {empty && !reduceMotion && prompts.length > 1 ? (
              <button
                type="button"
                className="class-ask-pause"
                aria-label={paused ? 'Resume suggestions' : 'Pause suggestions'}
                onClick={() => setPaused((value) => !value)}
              >
                {paused ? <Play aria-hidden="true" /> : <Pause aria-hidden="true" />}
              </button>
            ) : null}
            <span id={`${id}-hint`} className="text-text-tertiary text-xs">
              <span className="hidden sm:inline">Enter to send · Shift + Enter for a new line</span>
              <span className="sm:hidden">A question is a beginning.</span>
            </span>
          </div>
          <Button
            type="submit"
            disabled={!question.trim() || sending}
            className="h-11 shrink-0 gap-2 rounded-full px-5"
            aria-label="Ask"
          >
            {sending ? 'Opening…' : 'Ask Lyra'}
            <ArrowUp aria-hidden="true" className="size-4" />
          </Button>
        </div>
        {error ? (
          <p role="alert" className="mt-3 text-sm text-danger-text">
            Could not open the conversation. Your question is still here; try again.
          </p>
        ) : null}
      </form>
    </section>
  )
}
