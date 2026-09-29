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
  selectionRestore?: { anchor: number; focus: number } | null
  retryAction?: 'regenerate' | 'tutor-retry'
}

export type NativeChatSnapshot = {
  version: number
  scope: string
  rows: NativeChatRow[]
  agent: boolean
  dark: boolean
  liveGeneration?: string
}

export type NativeChatAction =
  | { kind: 'ready' }
  | { kind: 'content-ready'; scope: string }
  | { kind: 'overflow' }
  | { kind: 'retry'; action: 'regenerate' | 'tutor-retry' }
  | { kind: 'reveal-complete'; generation?: string }
  | { kind: 'reasoning-open'; open: boolean }
  | { kind: 'navigate'; href: string }
  | { kind: 'selection'; anchor: number; focus: number; generation?: string }

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
