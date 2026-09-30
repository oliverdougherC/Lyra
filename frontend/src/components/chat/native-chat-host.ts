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
  onRetry: (action: 'regenerate' | 'tutor-retry', rowKey: string) => void
  onRevealComplete: (generation?: string) => void
  onReasoningOpenChange: (open: boolean) => void
  onNavigate: (href: string) => void
  onSelection: (
    anchor: number | null,
    focus: number | null,
    generation?: string,
    rowKey?: string | null,
  ) => void
  onScrollState: (atBottom: boolean, ratio?: number) => void
}

type Owner = {
  hostId: string
  mounted: boolean
  failed: boolean
  readyScope: string | null
  version: number
  renderedVersion: number
  presentationId: number
  nextContentRevision: number
  contentByRow: Map<
    string,
    { generation?: string; content: string; epoch: number; revision: number }
  >
}
let nextHostId = 0
let mountQueue: Promise<unknown> = Promise.resolve()

// Mount and teardown share one native slot. Await the prior owner's teardown before
// another hook instance tries to claim it; the native boundary also checks hostId.
function queueLifetime(
  command: 'native_chat_mount' | 'native_chat_unmount',
  args: Record<string, unknown>,
) {
  const operation = mountQueue.then(() => callNativeChat(command, args))
  mountQueue = operation.catch(() => undefined)
  return operation
}

export function useNativeChatHost(
  enabled: boolean,
  snapshot: Omit<NativeChatSnapshot, 'hostId' | 'version' | 'dark'>,
  callbacks: Callbacks,
  activeRef: React.MutableRefObject<boolean>,
  occluded = false,
  getFallbackScrollRatio?: () => number | null,
) {
  const hostRef = useRef<HTMLDivElement>(null)
  const [active, setActive] = useState(false)
  const [failed, setFailed] = useState(false)
  const ownerRef = useRef<Owner | null>(null)
  const snapshotRef = useRef(snapshot)
  const callbacksRef = useRef(callbacks)
  const occludedRef = useRef(occluded)
  const ratioRef = useRef(getFallbackScrollRatio)
  const latestPayloadRef = useRef<NativeChatSnapshot | null>(null)
  const lastScopeRef = useRef(snapshot.scope)
  snapshotRef.current = snapshot
  callbacksRef.current = callbacks
  occludedRef.current = occluded
  ratioRef.current = getFallbackScrollRatio

  const belongs = useCallback((owner: Owner) => ownerRef.current === owner && !owner.failed, [])

  const fail = useCallback(
    (owner: Owner) => {
      if (!belongs(owner)) return
      owner.failed = true
      owner.mounted = false
      activeRef.current = false
      setActive(false)
      setFailed(true)
      void queueLifetime('native_chat_unmount', { hostId: owner.hostId }).catch(() => undefined)
    },
    [activeRef, belongs],
  )

  const hide = useCallback(
    (owner: Owner) => {
      if (!belongs(owner) || !owner.mounted) return
      activeRef.current = false
      setActive(false)
      const presentationId = ++owner.presentationId
      void callNativeChat('native_chat_hide', { hostId: owner.hostId, presentationId }).catch(
        () => {
          if (belongs(owner) && owner.presentationId === presentationId) fail(owner)
        },
      )
    },
    [activeRef, belongs, fail],
  )

  const show = useCallback(
    (owner: Owner, payload: NativeChatSnapshot) => {
      if (
        !belongs(owner) ||
        !owner.mounted ||
        occludedRef.current ||
        owner.readyScope !== payload.scope
      )
        return
      const presentationId = ++owner.presentationId
      const stillCurrent = () =>
        belongs(owner) &&
        owner.mounted &&
        !occludedRef.current &&
        owner.presentationId === presentationId &&
        owner.readyScope === payload.scope &&
        latestPayloadRef.current?.scope === payload.scope &&
        latestPayloadRef.current?.version === payload.version
      void (async () => {
        const ratio = ratioRef.current?.()
        if (ratio !== null && ratio !== undefined && Number.isFinite(ratio)) {
          await callNativeChat('native_chat_set_scroll_ratio', { hostId: owner.hostId, ratio })
        }
        if (!stillCurrent()) return
        await callNativeChat('native_chat_show', {
          hostId: owner.hostId,
          scope: payload.scope,
          version: payload.version,
          presentationId,
        })
        if (!stillCurrent()) return
        activeRef.current = true
        setActive(true)
      })().catch((error: unknown) => {
        if (String(error).includes('The chat content is not ready.')) return
        if (stillCurrent()) fail(owner)
      })
    },
    [activeRef, belongs, fail],
  )

  const sendSnapshot = useCallback(() => {
    const owner = ownerRef.current
    if (!owner || !belongs(owner) || !owner.mounted) return
    const currentRows = new Set<string>()
    const rows = snapshotRef.current.rows.map((row) => {
      if (!row.streaming) return row
      currentRows.add(row.key)
      const previous = owner.contentByRow.get(row.key)
      let epoch = previous?.epoch ?? 0
      let revision = previous?.revision ?? 0
      if (
        !previous ||
        previous.generation !== row.generation ||
        !row.message.content.startsWith(previous.content)
      ) {
        epoch = ++owner.nextContentRevision
        revision = epoch
      } else if (previous.content !== row.message.content) {
        revision = ++owner.nextContentRevision
      }
      owner.contentByRow.set(row.key, {
        generation: row.generation,
        content: row.message.content,
        epoch,
        revision,
      })
      return { ...row, contentEpoch: epoch, contentRevision: revision }
    })
    for (const key of owner.contentByRow.keys())
      if (!currentRows.has(key)) owner.contentByRow.delete(key)
    const payload: NativeChatSnapshot = {
      ...snapshotRef.current,
      rows,
      hostId: owner.hostId,
      version: ++owner.version,
      dark: document.documentElement.classList.contains('dark'),
    }
    latestPayloadRef.current = payload
    void callNativeChat('native_chat_render', { snapshot: payload }).then(
      () => {
        owner.renderedVersion = Math.max(owner.renderedVersion, payload.version)
        if (
          belongs(owner) &&
          owner.readyScope === payload.scope &&
          latestPayloadRef.current?.version === payload.version &&
          !activeRef.current
        )
          show(owner, payload)
      },
      () => {
        if (latestPayloadRef.current === payload) fail(owner)
      },
    )
  }, [activeRef, belongs, fail, show])

  useEffect(() => {
    if (!enabled || failed || !hasNativeChatBridge()) return
    const host = hostRef.current
    if (!host) return
    const nonce = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`
    const owner: Owner = {
      hostId: `chat-host-${++nextHostId}-${nonce}`,
      mounted: false,
      failed: false,
      readyScope: null,
      version: 0,
      renderedVersion: 0,
      presentationId: 0,
      nextContentRevision: 0,
      contentByRow: new Map(),
    }
    ownerRef.current = owner
    latestPayloadRef.current = null
    let frame: number | null = null
    const position = () => {
      frame = null
      if (!belongs(owner) || !owner.mounted) return
      void callNativeChat('native_chat_set_frame', {
        hostId: owner.hostId,
        rect: nativeChatRect(host),
      }).catch(() => fail(owner))
    }
    const schedulePosition = () => {
      if (frame === null) frame = requestAnimationFrame(position)
    }
    const observer = new ResizeObserver(schedulePosition)
    observer.observe(host)
    window.addEventListener('resize', schedulePosition)
    const stopActions = listenNativeChatAction((action: NativeChatAction) => {
      if (!belongs(owner) || action.hostId !== owner.hostId) return
      if (action.kind === 'ready') {
        if (owner.mounted) {
          owner.readyScope = null
          hide(owner)
          sendSnapshot()
        }
        return
      }
      const payload = latestPayloadRef.current
      if (
        !payload ||
        !owner.mounted ||
        action.scope !== payload.scope ||
        action.version > payload.version ||
        action.version < 1
      )
        return
      if (action.kind === 'content-ready') {
        owner.readyScope = payload.scope
        if (owner.renderedVersion === payload.version) show(owner, payload)
        return
      }
      if (action.kind === 'overflow') {
        fail(owner)
        return
      }
      if (action.kind === 'retry') {
        const row = payload.rows.find((item) => item.key === action.rowKey)
        if (row?.retryAction === action.action) callbacksRef.current.onRetry(action.action, row.key)
        return
      }
      if (action.kind === 'reveal-complete' || action.kind === 'reasoning-open') {
        const row = payload.rows.find((item) => item.key === action.rowKey)
        if (
          !row?.streaming ||
          !action.generation ||
          row.generation !== action.generation ||
          payload.liveGeneration !== action.generation ||
          row.contentEpoch === undefined ||
          row.contentEpoch !== action.contentEpoch ||
          (action.kind === 'reveal-complete' && row.contentRevision !== action.contentRevision)
        )
          return
        if (action.kind === 'reveal-complete')
          callbacksRef.current.onRevealComplete(action.generation)
        else callbacksRef.current.onReasoningOpenChange(action.open)
        return
      }
      if (action.kind === 'navigate' && payload.rows.some((item) => item.key === action.rowKey))
        callbacksRef.current.onNavigate(action.href)
      if (action.kind === 'selection') {
        const row =
          action.rowKey === null ? null : payload.rows.find((item) => item.key === action.rowKey)
        if (
          action.rowKey !== null &&
          (!row ||
            row.generation !== action.generation ||
            (row.streaming && action.version !== payload.version))
        ) {
          callbacksRef.current.onSelection(null, null, undefined, null)
          return
        }
        callbacksRef.current.onSelection(
          action.anchor ?? null,
          action.focus ?? null,
          action.generation,
          action.rowKey,
        )
      }
    })
    const onScrollState = (event: Event) => {
      const detail = (
        event as CustomEvent<{ hostId?: string; atBottom?: unknown; ratio?: unknown }>
      ).detail
      if (
        !belongs(owner) ||
        !activeRef.current ||
        detail?.hostId !== owner.hostId ||
        typeof detail.atBottom !== 'boolean'
      )
        return
      callbacksRef.current.onScrollState(
        detail.atBottom,
        typeof detail.ratio === 'number' ? detail.ratio : undefined,
      )
    }
    window.addEventListener('lyra:native-scroll-state', onScrollState)
    void queueLifetime('native_chat_mount', {
      hostId: owner.hostId,
      rect: nativeChatRect(host),
    }).then(
      () => {
        if (!belongs(owner)) return
        owner.mounted = true
        sendSnapshot()
      },
      () => fail(owner),
    )
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', schedulePosition)
      window.removeEventListener('lyra:native-scroll-state', onScrollState)
      stopActions()
      if (frame !== null) cancelAnimationFrame(frame)
      if (ownerRef.current === owner) ownerRef.current = null
      owner.mounted = false
      activeRef.current = false
      void queueLifetime('native_chat_unmount', { hostId: owner.hostId }).catch(() => undefined)
    }
  }, [activeRef, belongs, enabled, fail, failed, hide, sendSnapshot, show])

  useLayoutEffect(() => {
    if (snapshot.scope === lastScopeRef.current) return
    lastScopeRef.current = snapshot.scope
    setFailed(false)
    const owner = ownerRef.current
    if (owner) owner.readyScope = null
    activeRef.current = false
    setActive(false)
    if (owner) hide(owner)
  }, [activeRef, hide, snapshot.scope])

  useEffect(() => {
    if (ownerRef.current?.mounted) sendSnapshot()
  }, [snapshot, sendSnapshot])

  useLayoutEffect(() => {
    const owner = ownerRef.current
    if (!owner?.mounted || owner.failed) return
    if (occluded) {
      hide(owner)
    } else {
      const payload = latestPayloadRef.current
      if (payload && owner.readyScope === payload.scope) show(owner, payload)
    }
  }, [hide, occluded, show])

  useEffect(() => {
    if (!enabled) {
      setFailed(false)
      activeRef.current = false
      setActive(false)
    }
  }, [activeRef, enabled])

  useEffect(() => {
    if (!enabled || failed) return
    const observer = new MutationObserver(sendSnapshot)
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
    return () => observer.disconnect()
  }, [enabled, failed, sendSnapshot])

  const scrollToBottom = useCallback(async () => {
    const owner = ownerRef.current
    if (!owner || !belongs(owner) || !owner.mounted || occludedRef.current || !activeRef.current)
      return
    await callNativeChat('native_chat_scroll_to_bottom', { hostId: owner.hostId })
  }, [activeRef, belongs])

  return { hostRef, active: active && !occluded, scrollToBottom }
}
