import { act, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { NativeTranscript } from '@/native-transcript'
import type { NativeChatSnapshot } from '@/lib/native-chat'

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  delete window.__TAURI_INTERNALS__
})

it('forwards only the sidebar shortcut from a focused native transcript', async () => {
  const invoke = vi.fn(() => Promise.resolve())
  Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } })
  const hostId = 'chat-host-sidebar'
  const view = render(<NativeTranscript hostId={hostId} />)
  await act(async () =>
    window.__lyraNativeChatReceive?.({
      hostId,
      scope: 'class:sidebar',
      version: 2,
      rows: [],
      agent: true,
      dark: false,
    }),
  )
  invoke.mockClear()
  const send = (init: KeyboardEventInit, target: Element = view.getByRole('main')) => {
    const event = new KeyboardEvent('keydown', {
      key: 'b',
      bubbles: true,
      cancelable: true,
      ...init,
    })
    target.dispatchEvent(event)
    return event
  }
  for (const init of [{ metaKey: true }, { ctrlKey: true }]) {
    expect(send(init).defaultPrevented).toBe(true)
    expect(invoke).toHaveBeenLastCalledWith('native_chat_action', {
      action: { kind: 'toggle-sidebar', hostId, scope: 'class:sidebar', version: 2 },
    })
  }
  invoke.mockClear()
  for (const init of [
    {},
    { metaKey: true, key: 'c' },
    { metaKey: true, shiftKey: true },
    { ctrlKey: true, altKey: true },
    { metaKey: true, repeat: true },
    { metaKey: true, isComposing: true },
  ])
    expect(send(init).defaultPrevented).toBe(false)
  const field = document.createElement('textarea')
  view.getByRole('main').append(field)
  expect(send({ metaKey: true }, field).defaultPrevented).toBe(false)
  expect(invoke).not.toHaveBeenCalled()
  view.unmount()
  expect(send({ metaKey: true }, document.body).defaultPrevented).toBe(false)
  expect(invoke).not.toHaveBeenCalled()
})

it('routes unmodified reading keys to the native viewport while preserving editing and selection', async () => {
  const invoke = vi.fn(() => Promise.resolve())
  Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } })
  const hostId = 'chat-host-keyboard'
  const view = render(<NativeTranscript hostId={hostId} />)
  await act(async () =>
    window.__lyraNativeChatReceive?.({
      hostId,
      scope: 'class:keyboard',
      version: 4,
      rows: [],
      agent: true,
      dark: false,
    }),
  )
  const main = view.getByRole('main')
  const send = (key: string, init: KeyboardEventInit = {}, target: Element = main) => {
    const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init })
    target.dispatchEvent(event)
    return event
  }
  invoke.mockClear()
  for (const key of ['PageUp', 'PageDown', 'Home', 'End']) {
    expect(send(key).defaultPrevented).toBe(true)
    expect(invoke).toHaveBeenLastCalledWith('native_chat_action', {
      action: { kind: 'scroll-key', hostId, scope: 'class:keyboard', version: 4, key },
    })
  }
  expect(send('PageDown', { repeat: true }).defaultPrevented).toBe(true)
  invoke.mockClear()
  for (const init of [
    { shiftKey: true },
    { altKey: true },
    { metaKey: true },
    { ctrlKey: true },
    { isComposing: true },
  ])
    expect(send('Home', init).defaultPrevented).toBe(false)
  for (const key of [' ', 'ArrowUp', 'ArrowDown', 'Enter'])
    expect(send(key).defaultPrevented).toBe(false)
  const button = document.createElement('button')
  main.append(button)
  expect(send(' ', {}, button).defaultPrevented).toBe(false)
  button.remove()
  const handled = new KeyboardEvent('keydown', { key: 'Home', bubbles: true, cancelable: true })
  handled.preventDefault()
  main.dispatchEvent(handled)
  for (const tag of ['input', 'textarea', 'select']) {
    const field = document.createElement(tag)
    main.append(field)
    expect(send('Home', {}, field).defaultPrevented).toBe(false)
    field.remove()
  }
  const editable = document.createElement('div')
  editable.contentEditable = 'true'
  editable.setAttribute('contenteditable', 'true')
  main.append(editable)
  expect(send('End', {}, editable).defaultPrevented).toBe(false)
  editable.remove()
  const selected = document.createTextNode('selected answer')
  main.append(selected)
  const range = document.createRange()
  range.selectNodeContents(selected)
  window.getSelection()!.addRange(range)
  expect(send('Home').defaultPrevented).toBe(false)
  window.getSelection()!.removeAllRanges()
  expect(invoke).not.toHaveBeenCalled()
  view.unmount()
  expect(send('PageDown', {}, document.body).defaultPrevented).toBe(false)
  expect(invoke).not.toHaveBeenCalled()
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
