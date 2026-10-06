import type { ChatMessage } from '@/components/chat/message-bubble'
import type { ProcessingStage } from '@/components/chat/thinking-indicator'
import type { AgentChatActivity, WriterActivity } from '@/types'

export type NativeChatRow = {
  key: string
  message: ChatMessage
  className?: string
  startsTimeGap: boolean
  streaming?: boolean
  activity?: (AgentChatActivity | WriterActivity)[]
  processingStage?: ProcessingStage | null
  turnStartedAt?: number | null
  turnEnded?: boolean
  generation?: string
  contentEpoch?: number
  contentRevision?: number
  selectionRestore?: { anchor: number; focus: number } | null
  retryAction?: 'regenerate' | 'tutor-retry'
}

export type NativeChatSnapshot = {
  hostId: string
  version: number
  scope: string
  rows: NativeChatRow[]
  agent: boolean
  dark: boolean
  liveGeneration?: string
}

export type NativeChatAction =
  | { kind: 'ready'; hostId: string }
  | { kind: 'content-ready'; hostId: string; scope: string; version: number }
  | { kind: 'overflow'; hostId: string; scope: string; version: number }
  | { kind: 'toggle-sidebar'; hostId: string; scope: string; version: number }
  | {
      kind: 'scroll-key'
      hostId: string
      scope: string
      version: number
      key: 'PageUp' | 'PageDown' | 'Home' | 'End'
    }
  | {
      kind: 'retry'
      hostId: string
      scope: string
      version: number
      rowKey: string
      action: 'regenerate' | 'tutor-retry'
    }
  | {
      kind: 'reveal-complete'
      hostId: string
      scope: string
      version: number
      rowKey: string
      generation?: string
      contentEpoch?: number
      contentRevision?: number
    }
  | {
      kind: 'reasoning-open'
      hostId: string
      scope: string
      version: number
      rowKey: string
      generation?: string
      contentEpoch?: number
      open: boolean
    }
  | {
      kind: 'navigate'
      hostId: string
      scope: string
      version: number
      rowKey: string
      href: string
    }
  | {
      kind: 'selection'
      hostId: string
      scope: string
      version: number
      rowKey: string | null
      anchor?: number
      focus?: number
      generation?: string
    }

type NativeChatRect = { x: number; top: number; width: number; height: number }

function invoke() {
  return window.__TAURI_INTERNALS__?.invoke ?? window.__TAURI__?.core?.invoke
}

export function hasNativeChatBridge(): boolean {
  return typeof invoke() === 'function'
}

export async function callNativeChat(command: string, args?: Record<string, unknown>) {
  const native = invoke()
  if (!native) throw new Error('The native chat view is unavailable.')
  return native(command, args)
}

export function nativeChatRect(element: HTMLElement): NativeChatRect {
  const bounds = element.getBoundingClientRect()
  return { x: bounds.x, top: bounds.y, width: bounds.width, height: bounds.height }
}

export function listenNativeChatAction(onAction: (action: NativeChatAction) => void): () => void {
  const listener = (event: Event) => {
    const action = (event as CustomEvent<NativeChatAction>).detail
    if (action && typeof action === 'object' && 'kind' in action) onAction(action)
  }
  window.addEventListener('lyra:native-chat-action', listener)
  return () => window.removeEventListener('lyra:native-chat-action', listener)
}
