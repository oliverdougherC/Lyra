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

const childHostId = new URLSearchParams(window.location.search).get('hostId') ?? ''

function report(action: NativeChatAction) {
  void callNativeChat('native_chat_action', { action }).catch(() => undefined)
}

function selectedOffset(root: Element, node: Node, offset: number): number | null {
  if (!root.contains(node)) return null
  const range = document.createRange()
  range.selectNodeContents(root)
  try {
    range.setEnd(node, offset)
    return range.toString().length
  } catch {
    return null
  }
}

/** Resolve the selected row in this section; never borrow the live turn's identity. */
export function selectionFromTranscript(
  snapshot: NativeChatSnapshot,
  selection: Selection | null,
): Extract<NativeChatAction, { kind: 'selection' }> {
  const base = {
    kind: 'selection' as const,
    hostId: snapshot.hostId,
    scope: snapshot.scope,
    version: snapshot.version,
  }
  const clear = { ...base, rowKey: null }
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return clear
  const anchor = selection.anchorNode?.parentElement?.closest<HTMLElement>('[data-native-row-key]')
  const focus = selection.focusNode?.parentElement?.closest<HTMLElement>('[data-native-row-key]')
  if (!anchor || anchor !== focus) return clear
  const rowKey = anchor.dataset.nativeRowKey
  const row = snapshot.rows.find((item) => item.key === rowKey && item.message.role === 'assistant')
  const root = anchor.querySelector('.assistant-content')
  if (!row || !root || !selection.anchorNode || !selection.focusNode || !rowKey) return clear
  const anchorOffset = selectedOffset(root, selection.anchorNode, selection.anchorOffset)
  const focusOffset = selectedOffset(root, selection.focusNode, selection.focusOffset)
  if (anchorOffset === null || focusOffset === null) return clear
  return {
    ...base,
    rowKey,
    anchor: anchorOffset,
    focus: focusOffset,
    ...(row.generation ? { generation: row.generation } : {}),
  }
}

export function NativeTranscript({ hostId = childHostId }: { hostId?: string } = {}) {
  const [snapshot, setSnapshot] = useState<NativeChatSnapshot | null>(null)
  const versionRef = useRef(-1)
  const contentRef = useRef<HTMLElement>(null)
  const reportedHeightRef = useRef(0)
  const readyReportedRef = useRef(false)
  const heightRetriesRef = useRef(0)
  const heightRetryTimerRef = useRef<number | null>(null)
  const latestSnapshotRef = useRef<NativeChatSnapshot | null>(null)

  const clearHeightRetry = useCallback(() => {
    if (heightRetryTimerRef.current !== null) {
      window.clearTimeout(heightRetryTimerRef.current)
      heightRetryTimerRef.current = null
    }
  }, [])

  const publishHeight = useCallback(
    (height: number) => {
      const owner = latestSnapshotRef.current
      if (!owner) return
      clearHeightRetry()
      if (height > 18_000) {
        report({
          kind: 'overflow',
          hostId: owner.hostId,
          scope: owner.scope,
          version: owner.version,
        })
        return
      }
      if (Math.abs(height - reportedHeightRef.current) < 1 && readyReportedRef.current) return
      reportedHeightRef.current = height
      void callNativeChat('native_chat_set_content_height', {
        hostId: owner.hostId,
        scope: owner.scope,
        version: owner.version,
        height,
      }).then(
        () => {
          if (latestSnapshotRef.current !== owner) return
          heightRetriesRef.current = 0
          if (!readyReportedRef.current) {
            readyReportedRef.current = true
            report({
              kind: 'content-ready',
              hostId: owner.hostId,
              scope: owner.scope,
              version: owner.version,
            })
          }
        },
        () => {
          if (latestSnapshotRef.current !== owner) return
          reportedHeightRef.current = 0
          if (heightRetriesRef.current++ < 10) {
            clearHeightRetry()
            heightRetryTimerRef.current = window.setTimeout(() => {
              heightRetryTimerRef.current = null
              if (latestSnapshotRef.current === owner)
                publishHeight(contentRef.current?.scrollHeight ?? 0)
            }, 100)
          } else
            report({
              kind: 'overflow',
              hostId: owner.hostId,
              scope: owner.scope,
              version: owner.version,
            })
        },
      )
    },
    [clearHeightRetry],
  )

  useEffect(() => {
    window.__lyraNativeChatReceive = (incoming) => {
      if (incoming.hostId !== hostId || incoming.version <= versionRef.current) return
      clearHeightRetry()
      versionRef.current = incoming.version
      latestSnapshotRef.current = incoming
      readyReportedRef.current = false
      reportedHeightRef.current = 0
      heightRetriesRef.current = 0
      setSnapshot(incoming)
    }
    report({ kind: 'ready', hostId })
    return () => {
      delete window.__lyraNativeChatReceive
      latestSnapshotRef.current = null
      clearHeightRetry()
    }
  }, [clearHeightRetry, hostId])

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

  // Hidden WKWebViews may defer rAF. Publish the first populated height during commit.
  useLayoutEffect(() => {
    if (snapshot && contentRef.current) publishHeight(contentRef.current.scrollHeight)
  }, [snapshot, publishHeight])

  useEffect(() => {
    const intercept = (event: MouseEvent) => {
      if (!(event.target instanceof Element)) return
      const link = event.target.closest<HTMLAnchorElement>('a[href]')
      const owner = latestSnapshotRef.current
      const rowKey = link?.closest<HTMLElement>('[data-native-row-key]')?.dataset.nativeRowKey
      if (!link || !owner || !rowKey) return
      event.preventDefault()
      event.stopPropagation()
      report({
        kind: 'navigate',
        hostId: owner.hostId,
        scope: owner.scope,
        version: owner.version,
        rowKey,
        href: link.getAttribute('href') ?? '',
      })
    }
    document.addEventListener('click', intercept, true)
    return () => document.removeEventListener('click', intercept, true)
  }, [])

  useEffect(() => {
    let frame: number | null = null
    const reportSelection = () => {
      frame = null
      const owner = latestSnapshotRef.current
      if (owner) report(selectionFromTranscript(owner, window.getSelection()))
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
          <div key={row.key} data-native-row-key={row.key} className="contents">
            <MessageRow
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
                        hostId: snapshot.hostId,
                        scope: snapshot.scope,
                        version: snapshot.version,
                        rowKey: row.key,
                        action: row.retryAction as 'regenerate' | 'tutor-retry',
                      })
                  : undefined
              }
              onRevealComplete={
                row.streaming
                  ? (generation) =>
                      report({
                        kind: 'reveal-complete',
                        hostId: snapshot.hostId,
                        scope: snapshot.scope,
                        version: snapshot.version,
                        rowKey: row.key,
                        generation,
                        contentEpoch: row.contentEpoch,
                        contentRevision: row.contentRevision,
                      })
                  : undefined
              }
              onReasoningOpenChange={
                row.streaming
                  ? (open) =>
                      report({
                        kind: 'reasoning-open',
                        hostId: snapshot.hostId,
                        scope: snapshot.scope,
                        version: snapshot.version,
                        rowKey: row.key,
                        generation: row.generation,
                        contentEpoch: row.contentEpoch,
                        open,
                      })
                  : undefined
              }
            />
          </div>
        ))}
      </main>
    </TooltipProvider>
  )
}

const root = document.getElementById('native-transcript-root')
if (root) createRoot(root).render(<NativeTranscript />)
