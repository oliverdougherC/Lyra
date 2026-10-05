import { act, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { NativeTranscript } from '@/native-transcript'
import type { NativeChatSnapshot } from '@/lib/native-chat'

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  delete window.__TAURI_INTERNALS__
})

it('reports overflow after section-height recovery is exhausted so the host can restore DOM', async () => {
  vi.useFakeTimers()
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(100)
  const invoke = vi.fn((command: string) =>
    command === 'native_chat_set_content_height'
      ? Promise.reject(new Error('height rejected'))
      : Promise.resolve(),
  )
  Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } })
  const hostId = 'chat-host-test'
  const snapshot = {
    hostId,
    scope: 'class:one',
    version: 1,
    rows: [
      {
        key: 'reply',
        message: {
          id: 1,
          role: 'assistant',
          content: 'Answer',
          thinking: '',
          thinking_ms: 0,
          retrieval_trimmed: false,
          omitted_document_count: 0,
          tool_activity: [],
          created_at: '2026-08-04T12:00:00Z',
        },
        startsTimeGap: false,
      },
    ],
    agent: true,
    dark: false,
  } as NativeChatSnapshot
  render(<NativeTranscript hostId={hostId} />)
  await act(async () => window.__lyraNativeChatReceive?.(snapshot))
  for (let attempt = 0; attempt < 12; attempt++) {
    await act(async () => vi.advanceTimersByTime(100))
  }
  expect(invoke).toHaveBeenCalledWith('native_chat_action', {
    action: { kind: 'overflow', hostId, scope: 'class:one', version: 1 },
  })
})

it('cancels pending height recovery when the transcript is unmounted', async () => {
  vi.useFakeTimers()
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(100)
  const invoke = vi.fn((command: string) =>
    command === 'native_chat_set_content_height'
      ? Promise.reject(new Error('height rejected'))
      : Promise.resolve(),
  )
  Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } })
  const hostId = 'chat-host-unmount'
  const view = render(<NativeTranscript hostId={hostId} />)
  await act(async () =>
    window.__lyraNativeChatReceive?.({
      hostId,
      scope: 'class:one',
      version: 1,
      rows: [],
      agent: true,
      dark: false,
    }),
  )
  view.unmount()
  invoke.mockClear()
  await act(async () => vi.advanceTimersByTime(2000))
  expect(invoke).not.toHaveBeenCalled()
})

it('drops obsolete height retry timers when a newer snapshot has already been sized', async () => {
  vi.useFakeTimers()
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(100)
  const invoke = vi.fn((command: string, args?: { version?: number }) =>
    command === 'native_chat_set_content_height' && args?.version === 1
      ? Promise.reject(new Error('height rejected'))
      : Promise.resolve(),
  )
  Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } })
  const hostId = 'chat-host-replacement'
  const view = render(<NativeTranscript hostId={hostId} />)
  const snapshot: NativeChatSnapshot = {
    hostId,
    scope: 'class:one',
    version: 1,
    rows: [],
    agent: true,
    dark: false,
  }
  await act(async () => window.__lyraNativeChatReceive?.(snapshot))
  await act(async () => window.__lyraNativeChatReceive?.({ ...snapshot, version: 2 }))
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(120)
  invoke.mockClear()
  await act(async () => vi.advanceTimersByTime(100))
  expect(invoke).not.toHaveBeenCalled()
  view.unmount()
})
