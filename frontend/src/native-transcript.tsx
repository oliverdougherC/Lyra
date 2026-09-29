import '@/styles/katex.css'
import '@/styles/globals.css'

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'

import { MessageRow } from '@/components/chat/message-bubble'
import { TooltipProvider } from '@/components/ui/tooltip'
import { callNativeChat, type NativeChatAction, type NativeChatSnapshot } from '@/lib/native-chat'

declare global {
  interface Window {
    __lyraNativeChatReceive?: (snapshot: NativeChatSnapshot) => void
  }
}

function report(action: NativeChatAction) {
  void callNativeChat('native_chat_action', { action }).catch(() => undefined)
}

function NativeTranscript() {
  const [snapshot, setSnapshot] = useState<NativeChatSnapshot | null>(null)
  const versionRef = useRef(-1)
  const contentRef = useRef<HTMLElement>(null)
  const reportedHeightRef = useRef(0)
  const hasContentRef = useRef(false)
  const readyReportedRef = useRef(false)
  const heightRetriesRef = useRef(0)
  const latestSnapshotRef = useRef<NativeChatSnapshot | null>(null)

  const publishHeight = useCallback((height: number) => {
    if (height > 18_000) {
      report({ kind: 'overflow' })
      return
    }
    if (Math.abs(height - reportedHeightRef.current) < 1) return
    reportedHeightRef.current = height
    void callNativeChat('native_chat_set_content_height', { height }).then(
      () => {
        heightRetriesRef.current = 0
        if (hasContentRef.current && !readyReportedRef.current) {
          readyReportedRef.current = true
          report({ kind: 'content-ready', scope: latestSnapshotRef.current?.scope ?? '' })
        }
      },
      () => {
        reportedHeightRef.current = 0
        if (heightRetriesRef.current++ < 10)
          window.setTimeout(() => publishHeight(contentRef.current?.scrollHeight ?? 0), 100)
      },
    )
  }, [])

  useEffect(() => {
    window.__lyraNativeChatReceive = (incoming) => {
      if (incoming.version <= versionRef.current) return
      if (incoming.scope !== latestSnapshotRef.current?.scope) {
        readyReportedRef.current = false
        reportedHeightRef.current = 0
      }
      versionRef.current = incoming.version
      latestSnapshotRef.current = incoming
      hasContentRef.current = true
      setSnapshot(incoming)
    }
    report({ kind: 'ready' })
    return () => {
      delete window.__lyraNativeChatReceive
    }
  }, [])

  useEffect(() => {
    document.documentElement.classList.toggle('dark', Boolean(snapshot?.dark))
  }, [snapshot?.dark])

  useLayoutEffect(() => {
    const content = contentRef.current
    if (!content) return
    let frame: number | null = null
    const reportHeight = () => {
      frame = null
      publishHeight(content.scrollHeight)
    }
    const observer = new ResizeObserver(() => {
      if (frame === null) frame = requestAnimationFrame(reportHeight)
    })
    observer.observe(content)
    reportHeight()
    return () => {
      observer.disconnect()
      if (frame !== null) cancelAnimationFrame(frame)
    }
  }, [publishHeight])

  // The native view starts hidden, so its rAF callbacks may be deferred. Commit the
  // first populated height synchronously before asking AppKit to show the transcript.
  useLayoutEffect(() => {
    if (snapshot && contentRef.current) publishHeight(contentRef.current.scrollHeight)
  }, [snapshot, publishHeight])

  useEffect(() => {
    const intercept = (event: MouseEvent) => {
      if (!(event.target instanceof Element)) return
      const link = event.target.closest<HTMLAnchorElement>('a[href]')
      if (!link) return
      event.preventDefault()
      event.stopPropagation()
      report({ kind: 'navigate', href: link.getAttribute('href') ?? '' })
    }
    document.addEventListener('click', intercept, true)
    return () => document.removeEventListener('click', intercept, true)
  }, [])

  useEffect(() => {
    let frame: number | null = null
    const reportSelection = () => {
      frame = null
      const roots = document.querySelectorAll<HTMLElement>('.assistant-content')
      const root = roots[roots.length - 1]
      const selection = window.getSelection()
      if (!root || !selection || selection.rangeCount === 0) return
      const range = selection.getRangeAt(0)
      if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return
      const offsetOf = (node: Node, offset: number): number | null => {
        let total = 0
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
        for (let step = walker.nextNode(); step; step = walker.nextNode()) {
          if (step === node) return total + offset
          total += step.textContent?.length ?? 0
        }
        return null
      }
      const anchor = offsetOf(range.startContainer, range.startOffset)
      const focus = offsetOf(range.endContainer, range.endOffset)
      if (anchor !== null && focus !== null)
        report({
          kind: 'selection',
          anchor,
          focus,
          generation: latestSnapshotRef.current?.liveGeneration,
        })
    }
    const schedule = () => {
      if (frame === null) frame = requestAnimationFrame(reportSelection)
    }
    document.addEventListener('selectionchange', schedule)
    return () => {
      document.removeEventListener('selectionchange', schedule)
      if (frame !== null) cancelAnimationFrame(frame)
    }
  }, [])

  return (
    <TooltipProvider>
      <main
        ref={contentRef}
        className="mx-auto max-w-[860px] p-4 md:px-6"
        aria-label="Conversation"
      >
        {snapshot?.rows.map((row) => (
          <MessageRow
            key={row.key}
            message={row.message}
            className={row.className}
            startsTimeGap={row.startsTimeGap}
            streaming={row.streaming}
            activity={row.activity}
            agent={snapshot.agent}
            processingStage={row.processingStage}
            turnStartedAt={row.turnStartedAt}
            turnEnded={row.turnEnded}
            generation={row.generation}
            selectionRestore={row.selectionRestore}
            canRetry={Boolean(row.retryAction)}
            onRetry={
              row.retryAction
                ? () =>
                    report({
                      kind: 'retry',
                      action: row.retryAction as 'regenerate' | 'tutor-retry',
                    })
                : undefined
            }
            onRevealComplete={
              row.streaming
                ? (generation) => report({ kind: 'reveal-complete', generation })
                : undefined
            }
            onReasoningOpenChange={
              row.streaming ? (open) => report({ kind: 'reasoning-open', open }) : undefined
            }
          />
        ))}
      </main>
    </TooltipProvider>
  )
}

const root = document.getElementById('native-transcript-root')
if (root) createRoot(root).render(<NativeTranscript />)
