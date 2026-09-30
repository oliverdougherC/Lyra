import { act, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useNativeChatHost } from '@/components/chat/native-chat-host'
import type { NativeChatAction, NativeChatRow, NativeChatSnapshot } from '@/lib/native-chat'

type Snapshot = Omit<NativeChatSnapshot, 'hostId' | 'version' | 'dark'>

const row = (key: string, retryAction?: 'regenerate'): NativeChatRow =>
  ({ key, retryAction, message: { id: 1, role: 'assistant', content: key } }) as NativeChatRow

function deferred() {
  let resolve!: () => void
  let reject!: (error: Error) => void
  const promise = new Promise<void>((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}

const action = (value: NativeChatAction) =>
  window.dispatchEvent(new CustomEvent('lyra:native-chat-action', { detail: value }))

function harness(initial: Snapshot) {
  const calls: { command: string; args: Record<string, unknown> }[] = []
  const pending = new Map<string, ReturnType<typeof deferred>>()
  const invoke = vi.fn((command: string, args: Record<string, unknown> = {}) => {
    calls.push({ command, args })
    return pending.get(command)?.promise ?? Promise.resolve()
  })
  Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } })
  const callbacks = {
    onRetry: vi.fn(),
    onRevealComplete: vi.fn(),
    onReasoningOpenChange: vi.fn(),
    onNavigate: vi.fn(),
    onSelection: vi.fn(),
    onScrollState: vi.fn(),
  }
  const activeRef = { current: false }
  let current: ReturnType<typeof useNativeChatHost>
  function Host({ snapshot, occluded }: { snapshot: Snapshot; occluded: boolean }) {
    current = useNativeChatHost(true, snapshot, callbacks, activeRef, occluded)
    return <div ref={current.hostRef} />
  }
  const result = render(<Host snapshot={initial} occluded={false} />)
  const last = (command: string) => calls.filter((call) => call.command === command).at(-1)
  return {
    ...result,
    rerender: (props: { snapshot: Snapshot; occluded: boolean }) =>
      result.rerender(<Host {...props} />),
    current: () => current,
    calls,
    pending,
    callbacks,
    activeRef,
    last,
  }
}

describe('native chat host ownership', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    delete window.__TAURI_INTERNALS__
  })

  it('ignores a show acknowledgment after overflow and tears the view down', async () => {
    const snapshot: Snapshot = { scope: 'class:one', rows: [row('a')], agent: true }
    const h = harness(snapshot)
    await waitFor(() => expect(h.last('native_chat_render')).toBeTruthy())
    const sent = h.last('native_chat_render')!.args.snapshot as NativeChatSnapshot
    const show = deferred()
    h.pending.set('native_chat_show', show)
    act(() =>
      action({
        kind: 'content-ready',
        hostId: sent.hostId,
        scope: sent.scope,
        version: sent.version,
      }),
    )
    await waitFor(() => expect(h.last('native_chat_show')).toBeTruthy())
    act(() =>
      action({ kind: 'overflow', hostId: sent.hostId, scope: sent.scope, version: sent.version }),
    )
    await waitFor(() => expect(h.last('native_chat_unmount')).toBeTruthy())
    await act(async () => show.resolve())
    expect(h.current().active).toBe(false)
    expect(h.activeRef.current).toBe(false)
    h.unmount()
  })

  it('rejects stale ready/retry from the former scope and accepts only the matching row', async () => {
    const old: Snapshot = { scope: 'class:one', rows: [row('old', 'regenerate')], agent: true }
    const h = harness(old)
    await waitFor(() => expect(h.last('native_chat_render')).toBeTruthy())
    const first = h.last('native_chat_render')!.args.snapshot as NativeChatSnapshot
    h.rerender({
      snapshot: { scope: 'class:two', rows: [row('new', 'regenerate')], agent: true },
      occluded: false,
    })
    await waitFor(() =>
      expect((h.last('native_chat_render')!.args.snapshot as NativeChatSnapshot).scope).toBe(
        'class:two',
      ),
    )
    const next = h.last('native_chat_render')!.args.snapshot as NativeChatSnapshot
    act(() => {
      action({
        kind: 'content-ready',
        hostId: first.hostId,
        scope: first.scope,
        version: first.version,
      })
      action({
        kind: 'retry',
        hostId: first.hostId,
        scope: first.scope,
        version: first.version,
        rowKey: 'old',
        action: 'regenerate',
      })
      action({
        kind: 'retry',
        hostId: next.hostId,
        scope: next.scope,
        version: next.version,
        rowKey: 'old',
        action: 'regenerate',
      })
    })
    expect(h.last('native_chat_show')).toBeUndefined()
    expect(h.callbacks.onRetry).not.toHaveBeenCalled()
    act(() =>
      action({
        kind: 'retry',
        hostId: next.hostId,
        scope: next.scope,
        version: next.version,
        rowKey: 'new',
        action: 'regenerate',
      }),
    )
    expect(h.callbacks.onRetry).toHaveBeenCalledTimes(1)
    h.unmount()
  })

  it('ignores queued scroll state after switching conversations', async () => {
    const snapshot: Snapshot = { scope: 'class:one', rows: [row('a')], agent: true }
    const h = harness(snapshot)
    await waitFor(() => expect(h.last('native_chat_render')).toBeTruthy())
    const first = h.last('native_chat_render')!.args.snapshot as NativeChatSnapshot
    act(() =>
      action({
        kind: 'content-ready',
        hostId: first.hostId,
        scope: first.scope,
        version: first.version,
      }),
    )
    await waitFor(() => expect(h.current().active).toBe(true))
    h.rerender({ snapshot: { scope: 'class:two', rows: [row('b')], agent: true }, occluded: false })
    act(() =>
      window.dispatchEvent(
        new CustomEvent('lyra:native-scroll-state', {
          detail: { hostId: first.hostId, atBottom: false, ratio: 0.25 },
        }),
      ),
    )
    expect(h.callbacks.onScrollState).not.toHaveBeenCalled()
    h.unmount()
  })

  it('unmounts on frame rejection and keeps the fallback visible', async () => {
    const h = harness({ scope: 'class:one', rows: [row('a')], agent: true })
    await waitFor(() => expect(h.last('native_chat_render')).toBeTruthy())
    const sent = h.last('native_chat_render')!.args.snapshot as NativeChatSnapshot
    act(() =>
      action({
        kind: 'content-ready',
        hostId: sent.hostId,
        scope: sent.scope,
        version: sent.version,
      }),
    )
    await waitFor(() => expect(h.current().active).toBe(true))
    const frame = deferred()
    h.pending.set('native_chat_set_frame', frame)
    await act(async () => window.dispatchEvent(new Event('resize')))
    await waitFor(() => expect(h.last('native_chat_set_frame')).toBeTruthy())
    await act(async () => frame.reject(new Error('frame failed')))
    await waitFor(() => expect(h.last('native_chat_unmount')).toBeTruthy())
    expect(h.current().active).toBe(false)
    h.unmount()
  })

  it('keeps the mounted transcript hidden under an overlay and restores it afterward', async () => {
    const snapshot: Snapshot = { scope: 'class:one', rows: [row('a')], agent: true }
    const h = harness(snapshot)
    await waitFor(() => expect(h.last('native_chat_render')).toBeTruthy())
    const sent = h.last('native_chat_render')!.args.snapshot as NativeChatSnapshot
    act(() =>
      action({
        kind: 'content-ready',
        hostId: sent.hostId,
        scope: sent.scope,
        version: sent.version,
      }),
    )
    await waitFor(() => expect(h.current().active).toBe(true))
    h.rerender({ snapshot, occluded: true })
    await waitFor(() => expect(h.last('native_chat_hide')?.args.hostId).toBe(sent.hostId))
    expect(h.current().active).toBe(false)
    expect(h.last('native_chat_unmount')).toBeUndefined()
    h.rerender({ snapshot, occluded: false })
    await waitFor(() => expect(h.current().active).toBe(true))
    expect(h.last('native_chat_show')?.args).toEqual({
      hostId: sent.hostId,
      scope: sent.scope,
      version: sent.version,
      presentationId: expect.any(Number),
    })
    h.unmount()
  })

  it('falls back and tears down after a large-history render rejection', async () => {
    const tooMany: Snapshot = {
      scope: 'class:one',
      rows: Array.from({ length: 2049 }, (_, i) => row(`row-${i}`)),
      agent: true,
    }
    const h = harness(tooMany)
    const render = deferred()
    h.pending.set('native_chat_render', render)
    await waitFor(() => expect(h.last('native_chat_render')).toBeTruthy())
    await act(async () =>
      render.reject(new Error('The chat contains too many rows for the native view.')),
    )
    await waitFor(() => expect(h.last('native_chat_unmount')).toBeTruthy())
    expect(h.current().active).toBe(false)
    h.unmount()
  })

  it('sends Jump to latest only to the active unoccluded owner', async () => {
    const snapshot: Snapshot = { scope: 'class:one', rows: [row('a')], agent: true }
    const h = harness(snapshot)
    await waitFor(() => expect(h.last('native_chat_render')).toBeTruthy())
    const sent = h.last('native_chat_render')!.args.snapshot as NativeChatSnapshot
    await act(async () => h.current().scrollToBottom())
    expect(h.last('native_chat_scroll_to_bottom')).toBeUndefined()
    act(() =>
      action({
        kind: 'content-ready',
        hostId: sent.hostId,
        scope: sent.scope,
        version: sent.version,
      }),
    )
    await waitFor(() => expect(h.current().active).toBe(true))
    await act(async () => h.current().scrollToBottom())
    expect(h.last('native_chat_scroll_to_bottom')?.args.hostId).toBe(sent.hostId)
    h.rerender({ snapshot, occluded: true })
    const count = h.calls.filter((call) => call.command === 'native_chat_scroll_to_bottom').length
    await act(async () => h.current().scrollToBottom())
    expect(h.calls.filter((call) => call.command === 'native_chat_scroll_to_bottom')).toHaveLength(
      count,
    )
    h.unmount()
  })

  it('rejects a queued link from a row replaced within the same conversation', async () => {
    const first: Snapshot = { scope: 'class:one', rows: [row('old')], agent: true }
    const h = harness(first)
    await waitFor(() => expect(h.last('native_chat_render')).toBeTruthy())
    const old = h.last('native_chat_render')!.args.snapshot as NativeChatSnapshot
    h.rerender({ snapshot: { ...first, rows: [row('new')] }, occluded: false })
    await waitFor(() =>
      expect(
        (h.last('native_chat_render')!.args.snapshot as NativeChatSnapshot).version,
      ).toBeGreaterThan(old.version),
    )
    const current = h.last('native_chat_render')!.args.snapshot as NativeChatSnapshot
    act(() => {
      action({
        kind: 'navigate',
        hostId: old.hostId,
        scope: old.scope,
        version: old.version,
        rowKey: 'old',
        href: '/old',
      })
      action({
        kind: 'navigate',
        hostId: current.hostId,
        scope: current.scope,
        version: current.version,
        rowKey: 'new',
        href: '/new',
      })
    })
    expect(h.callbacks.onNavigate).toHaveBeenCalledOnce()
    expect(h.callbacks.onNavigate).toHaveBeenCalledWith('/new')
    h.unmount()
  })

  it('orders a delayed show before the newer hide on scope change', async () => {
    const first: Snapshot = { scope: 'class:one', rows: [row('a')], agent: true }
    const h = harness(first)
    await waitFor(() => expect(h.last('native_chat_render')).toBeTruthy())
    const sent = h.last('native_chat_render')!.args.snapshot as NativeChatSnapshot
    const delayedShow = deferred()
    h.pending.set('native_chat_show', delayedShow)
    act(() =>
      action({
        kind: 'content-ready',
        hostId: sent.hostId,
        scope: sent.scope,
        version: sent.version,
      }),
    )
    await waitFor(() => expect(h.last('native_chat_show')).toBeTruthy())
    const showId = h.last('native_chat_show')!.args.presentationId as number
    h.rerender({ snapshot: { ...first, scope: 'class:two' }, occluded: false })
    await waitFor(() => expect(h.last('native_chat_hide')).toBeTruthy())
    const hideId = h.last('native_chat_hide')!.args.presentationId as number
    expect(Number.isSafeInteger(showId)).toBe(true)
    expect(hideId).toBeGreaterThan(showId)
    await act(async () => delayedShow.resolve())
    expect(h.current().active).toBe(false)
    expect(h.activeRef.current).toBe(false)
    h.unmount()
  })
})
