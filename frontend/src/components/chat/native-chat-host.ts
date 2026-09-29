import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

import {
  callNativeChat,
  hasNativeChatBridge,
  listenNativeChatAction,
  nativeChatRect,
  type NativeChatAction,
  type NativeChatSnapshot,
} from '@/lib/native-chat'

type Callbacks = {
  onRetry: (action: 'regenerate' | 'tutor-retry') => void
  onRevealComplete: (generation?: string) => void
  onReasoningOpenChange: (open: boolean) => void
  onNavigate: (href: string) => void
  onSelection: (anchor: number, focus: number, generation?: string) => void
  onScrollState: (atBottom: boolean) => void
}

export function useNativeChatHost(
  enabled: boolean,
  snapshot: Omit<NativeChatSnapshot, 'version' | 'dark'>,
  callbacks: Callbacks,
  activeRef: React.MutableRefObject<boolean>,
) {
  const hostRef = useRef<HTMLDivElement>(null)
  const [active, setActive] = useState(false)
  const mountedRef = useRef(false)
  const failedRef = useRef(false)
  const snapshotRef = useRef(snapshot)
  const callbacksRef = useRef(callbacks)
  const versionRef = useRef(0)
  const activeScopeRef = useRef(snapshot.scope)
  snapshotRef.current = snapshot
  callbacksRef.current = callbacks

  const sendSnapshot = useCallback(() => {
    if (!mountedRef.current) return
    const payload: NativeChatSnapshot = {
      ...snapshotRef.current,
      version: ++versionRef.current,
      dark: document.documentElement.classList.contains('dark'),
    }
    void callNativeChat('native_chat_render', { snapshot: payload }).catch(() => {
      failedRef.current = true
      activeRef.current = false
      setActive(false)
      void callNativeChat('native_chat_unmount').catch(() => undefined)
    })
  }, [activeRef])

  useEffect(() => {
    if (!enabled || failedRef.current) return
    const stopActions = listenNativeChatAction((action: NativeChatAction) => {
      switch (action.kind) {
        case 'ready':
          sendSnapshot()
          break
        case 'content-ready':
          if (!mountedRef.current || activeRef.current) return
          void callNativeChat('native_chat_show').then(
            () => {
              activeRef.current = true
              activeScopeRef.current = snapshotRef.current.scope
              setActive(true)
            },
            () => {
              failedRef.current = true
            },
          )
          break
        case 'overflow':
          failedRef.current = true
          activeRef.current = false
          setActive(false)
          void callNativeChat('native_chat_unmount').catch(() => undefined)
          break
        case 'retry':
          callbacksRef.current.onRetry(action.action)
          break
        case 'reveal-complete':
          callbacksRef.current.onRevealComplete(action.generation)
          break
        case 'reasoning-open':
          callbacksRef.current.onReasoningOpenChange(action.open)
          break
        case 'navigate':
          callbacksRef.current.onNavigate(action.href)
          break
        case 'selection':
          callbacksRef.current.onSelection(action.anchor, action.focus, action.generation)
          break
      }
    })
    const onScrollState = (event: Event) => {
      const atBottom = (event as CustomEvent<{ atBottom?: unknown }>).detail?.atBottom
      if (typeof atBottom === 'boolean') callbacksRef.current.onScrollState(atBottom)
    }
    window.addEventListener('lyra:native-scroll-state', onScrollState)
    return () => {
      stopActions()
      window.removeEventListener('lyra:native-scroll-state', onScrollState)
    }
  }, [activeRef, enabled, sendSnapshot])

  useEffect(() => {
    if (!enabled || failedRef.current || !hasNativeChatBridge()) return
    const host = hostRef.current
    if (!host) return
    let cancelled = false
    let frame: number | null = null
    const position = () => {
      frame = null
      if (!mountedRef.current) return
      void callNativeChat('native_chat_set_frame', { rect: nativeChatRect(host) }).catch(() => {
        failedRef.current = true
        activeRef.current = false
        setActive(false)
      })
    }
    const schedulePosition = () => {
      if (frame === null) frame = requestAnimationFrame(position)
    }
    const observer = new ResizeObserver(schedulePosition)
    observer.observe(host)
    window.addEventListener('resize', schedulePosition)
    void callNativeChat('native_chat_mount', { rect: nativeChatRect(host) }).then(
      () => {
        mountedRef.current = true
        if (cancelled) {
          void callNativeChat('native_chat_unmount').catch(() => undefined)
          return
        }
        sendSnapshot()
      },
      () => {
        failedRef.current = true
      },
    )
    return () => {
      cancelled = true
      observer.disconnect()
      window.removeEventListener('resize', schedulePosition)
      if (frame !== null) cancelAnimationFrame(frame)
      mountedRef.current = false
      activeRef.current = false
      void callNativeChat('native_chat_unmount').catch(() => undefined)
    }
  }, [activeRef, enabled, sendSnapshot])

  useEffect(() => {
    if (mountedRef.current) sendSnapshot()
  }, [snapshot, sendSnapshot])

  useLayoutEffect(() => {
    if (snapshot.scope === activeScopeRef.current) return
    activeRef.current = false
    setActive(false)
  }, [activeRef, snapshot.scope])

  useEffect(() => {
    if (!enabled && active) setActive(false)
  }, [active, enabled])

  useEffect(() => {
    if (!enabled) return
    const observer = new MutationObserver(sendSnapshot)
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
    return () => observer.disconnect()
  }, [enabled, sendSnapshot])

  return { hostRef, active }
}
