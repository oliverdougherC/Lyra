/**
 * The draft workspace's save lifecycle (PLA-513): what happens to a pending or failing
 * write when the editing surface suspends or goes away.
 *
 * - A hidden tab flushes and suspends: the failing write settles, but no background retry
 *   timer may keep probing a dead endpoint while the window is hidden. Returning to
 *   visible resumes, and the owed retry lands on the failure backoff, not the typing
 *   debounce.
 * - An unmount disposes: the final flush's write starts synchronously and may still settle
 *   late - it applies its data, but can never report state or re-arm an ownerless timer.
 * - The beforeunload guard warns while the engine still has unconfirmed state.
 *
 * The mocks mirror `draft-workspace-polish.test.tsx`; the differences are deliberate -
 * fake timers throughout (the engine's ownership of real timers is the point under test)
 * and `fireEvent` in place of `userEvent`, whose internal delays do not mix with the fake
 * clock.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import DraftWorkspacePage from '@/app/classes/[id]/drafts/[artifactId]/page'
import { RouterProvider } from '@/router/hooks'
import { retryDelay } from '@/lib/drafts/save-engine'
import { saveSessionEngine } from '@/lib/drafts/save-session'
import { assertUpdateSafe } from '@/lib/update-safety'
import type { LiveDraftSuggestion } from '@/types'

const controls = vi.hoisted(() => ({
  save: vi.fn(),
  print: vi.fn(),
  exportAvailable: true,
  start: vi.fn(),
  state: 'ready',
  draftError: false,
  retryDraft: vi.fn(),
  wide: false,
  live: null as LiveDraftSuggestion | null,
  artifactId: '7',
  draft: {
    id: 7,
    class_id: 1,
    part_id: 8,
    title: 'My essay',
    body: 'Original body',
    body_version: 1,
    state: 'ready',
    stage_detail: null as string | null,
    error_message: null,
  },
  comments: [] as Array<{ id: number; body: string; resolved: boolean; section_ref: string }>,
}))
vi.mock('@/router/hooks', async (original) => ({
  ...(await original<object>()),
  useParams: () => ({ id: '1', artifactId: controls.artifactId }),
}))
vi.mock('@/router/dynamic', () => ({
  default: () =>
    function EditorMock(props: {
      initialMarkdown: string
      onChange: (value: string) => void
      onEditorReady: (view: unknown) => void
    }) {
      return (
        <textarea
          aria-label="Editor"
          defaultValue={props.initialMarkdown}
          onFocus={(e) => props.onEditorReady({ dom: e.currentTarget })}
          onChange={(e) => props.onChange(e.target.value)}
        />
      )
    },
}))
vi.mock('@/components/layout/page-chrome', () => ({
  useFullBleed: vi.fn(),
  useImmersiveChrome: vi.fn(),
  HeaderCrumb: () => null,
}))
vi.mock('@/lib/hooks/use-media-query', () => ({
  useMediaQuery: (query: string) => query === '(min-width: 1280px)' && controls.wide,
}))
vi.mock('@/lib/hooks/use-classes', () => ({
  useClasses: () => ({ data: [{ id: 1, name: 'History' }] }),
}))
vi.mock('@/lib/hooks/use-drafts', async (original) => ({
  ...(await original<object>()),
  useDraft: () => ({
    data: controls.draft,
    isError: controls.draftError,
    refetch: controls.retryDraft,
  }),
  useDraftStatus: () => ({ data: { state: controls.state } }),
  usePendingEdit: () => ({ data: null }),
  useComments: () => ({ data: controls.comments }),
  useWriterSessions: () => ({ data: [] }),
  useExportAvailability: () => ({ data: { available: controls.exportAvailable } }),
  useLiveDraftSuggestion: () => ({ data: controls.live }),
  useStartPass: () => ({ mutateAsync: controls.start, isPending: false }),
  useUpdateBody: () => ({ mutateAsync: controls.save, isPending: false }),
}))
vi.mock('@/lib/runtime', async (original) => ({
  ...(await original<object>()),
  printCurrentDocument: controls.print,
}))
vi.mock('@/components/drafts/brief-card', () => ({ BriefCard: () => null }))
vi.mock('@/components/drafts/source-ledger', () => ({ SourceLedger: () => <p>Source list</p> }))
vi.mock('@/components/drafts/plan-panel', async () => {
  const { useState } = await import('react')
  return {
    PlanPanel: () => {
      const [text, setText] = useState('Original plan')
      return (
        <textarea aria-label="Plan thesis" value={text} onChange={(e) => setText(e.target.value)} />
      )
    },
  }
})
vi.mock('@/components/drafts/comment-list', () => ({
  CommentList: (props: {
    onAddressComment: (comment: unknown) => Promise<void>
    addressingDisabled: boolean
  }) => (
    <button
      disabled={props.addressingDisabled}
      onClick={() => void props.onAddressComment(controls.comments[0])}
    >
      Address comment
    </button>
  ),
}))
vi.mock('@/components/chat/chat-pane', () => ({ ChatPane: () => <p>Assistant composer</p> }))
vi.mock('@/components/solutions/revision-history', () => ({
  RevisionHistory: ({ part }: { part: unknown }) => (part ? <p>History opened</p> : null),
}))

function renderWorkspace() {
  window.history.replaceState({}, '', '/#/classes/1/drafts/7')
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const contents = () => (
    <QueryClientProvider client={client}>
      <RouterProvider>
        <DraftWorkspacePage />
      </RouterProvider>
    </QueryClientProvider>
  )
  const view = render(contents())
  return { ...view, refresh: () => view.rerender(contents()) }
}

/** The production page in dev mode: React StrictMode's setup/cleanup/setup on every effect. */
function renderWorkspaceStrict() {
  window.history.replaceState({}, '', '/#/classes/1/drafts/7')
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const contents = () => (
    <QueryClientProvider client={client}>
      <RouterProvider>
        <StrictMode>
          <DraftWorkspacePage />
        </StrictMode>
      </RouterProvider>
    </QueryClientProvider>
  )
  return render(contents())
}

/** Mount the workspace with the editor focused (the page seeds the engine on ready). */
async function renderWithEditor(): Promise<ReturnType<typeof renderWorkspace>> {
  const view = renderWorkspace()
  await act(async () => {
    fireEvent.focus(screen.getByRole('textbox', { name: 'Editor' }))
  })
  return view
}

/** Report the textarea's new value through the editor's onChange, as typing would. */
function typeInto(value: string): void {
  // The ready callback re-labels the editor 'Draft document', so after focus the
  // accessible name is that one.
  const editor = screen.getByRole('textbox', { name: 'Draft document' })
  act(() => {
    fireEvent.change(editor, { target: { value } })
  })
}

async function setVisibility(state: 'visible' | 'hidden'): Promise<void> {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true })
  await act(async () => {
    document.dispatchEvent(new Event('visibilitychange'))
  })
}

let nextArtifactId = 7

beforeEach(() => {
  vi.useFakeTimers()
  // Each test gets its own document id so that a retained session (a pending or failing
  // save) does not contaminate the next test. A remount flow within the same test reuses
  // the same id (the session is keyed to the document, not per test).
  controls.artifactId = String(++nextArtifactId)
  controls.save.mockReset().mockResolvedValue({ version: 2 })
  controls.print.mockReset().mockResolvedValue(undefined)
  controls.exportAvailable = true
  controls.start.mockReset().mockResolvedValue({})
  controls.draftError = false
  controls.retryDraft.mockReset()
  controls.draft.stage_detail = null
  controls.state = 'ready'
  controls.live = null
  controls.wide = false
  window.localStorage.clear()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.setPointerCapture = vi.fn()
  Element.prototype.releasePointerCapture = vi.fn()
  Element.prototype.scrollIntoView = vi.fn()
})

afterEach(() => {
  vi.useRealTimers()
  Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
})

describe('hidden tab: flush, suspend, bounded retry (PLA-513)', () => {
  const failingSave = () => {
    let saveCalls = 0
    controls.save.mockImplementation(() => {
      saveCalls += 1
      if (saveCalls === 1) return Promise.reject(new Error('offline'))
      return Promise.resolve({ version: 3 })
    })
  }

  it('keeps the pending content, stops background retries while hidden, and fires the overdue retry at once on return', async () => {
    failingSave()

    await renderWithEditor()
    typeInto('Original body, revised')
    // The healthy debounce timer is armed; the flush on hide starts the write now.
    await setVisibility('hidden')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(controls.save).toHaveBeenCalledTimes(1)
    expect(controls.save).toHaveBeenCalledWith({
      content: 'Original body, revised',
      expected_version: 1,
    })

    // The failure has settled while suspended: nothing is owed to a timer, so an hour of
    // hidden time probes the dead endpoint zero more times.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_600_000)
    })
    expect(controls.save).toHaveBeenCalledTimes(1)

    // Back to visible, long past the retry's deadline: the wait was already paid for in
    // hidden time, so the owed retry fires at once - it is never restarted from the
    // backoff base, and the second attempt succeeds.
    await setVisibility('visible')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(controls.save).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('status')).toHaveTextContent('Saved')
  })

  it('waits only the remaining backoff when the tab returns before the retry is due', async () => {
    failingSave()

    await renderWithEditor()
    typeInto('Original body, revised')
    await setVisibility('hidden') // flush starts write #1, which fails
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(controls.save).toHaveBeenCalledTimes(1)

    // Return after half the backoff: the retry owes its remaining 1.5s, not a fresh 2s.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500)
    })
    await setVisibility('visible')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    expect(controls.save).toHaveBeenCalledTimes(1) // 1.5s after the failure: too early
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500)
    })
    expect(controls.save).toHaveBeenCalledTimes(2) // the failure's own deadline
    expect(screen.getByRole('status')).toHaveTextContent('Saved')
  })
})

describe('unmount: dispose is terminal (PLA-513)', () => {
  it('lets the final flush settle late, retires the session, and leaves no retry owner', async () => {
    let finishSave!: (value: { version: number }) => void
    controls.save.mockImplementation(
      () =>
        new Promise((resolve) => {
          finishSave = resolve
        }),
    )

    const view = await renderWithEditor()
    typeInto('Original body, leaving')
    // Unmount: the cleanup detaches (starting the write synchronously) and suspends.
    view.unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(controls.save).toHaveBeenCalledTimes(1)
    expect(controls.save).toHaveBeenCalledWith({
      content: 'Original body, leaving',
      expected_version: 1,
    })

    // The write settles long after the surface is gone: its data applies, no timer is
    // re-armed, and the final flush confirms the save so the session is retired -
    // a rapid remount seeds from the query, not a retained engine.
    await act(async () => {
      finishSave({ version: 2 })
      await vi.advanceTimersByTimeAsync(10 * 60_000)
    })
    expect(controls.save).toHaveBeenCalledTimes(1) // no retry of the settled write
    expect(saveSessionEngine(controls.artifactId)).toBeNull() // session released
    expect(vi.getTimerCount()).toBe(0) // no background retry owner after detachment
  })
})

describe('the beforeunload guard', () => {
  it('warns while the engine has unconfirmed state and stays quiet when clean', async () => {
    await renderWithEditor()

    // Clean: nothing unconfirmed, no warning.
    const quiet = new Event('beforeunload', { cancelable: true })
    act(() => {
      window.dispatchEvent(quiet)
    })
    expect(quiet.defaultPrevented).toBe(false)

    // Dirty: a revision is waiting in the engine's debounce.
    typeInto('Original body, leaving')
    const warned = new Event('beforeunload', { cancelable: true })
    act(() => {
      window.dispatchEvent(warned)
    })
    expect(warned.defaultPrevented).toBe(true)
  })
})

describe('StrictMode double effect (PLA-513)', () => {
  it('re-attaches a fresh engine after the dev re-run, still autosaves, and flushes once at teardown', async () => {
    const view = renderWorkspaceStrict()
    await act(async () => {
      fireEvent.focus(screen.getByRole('textbox', { name: 'Editor' }))
    })

    // StrictMode's setup/cleanup/setup disposed the first engine on the spot; the page
    // must attach a fresh one, or every edit below would land in a terminal engine and
    // never save. The autosave through the re-attached engine is the regression.
    typeInto('Original body, strict')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500)
    })
    expect(controls.save).toHaveBeenCalledTimes(1)
    expect(controls.save).toHaveBeenCalledWith({
      content: 'Original body, strict',
      expected_version: 1,
    })
    expect(screen.getByRole('status')).toHaveTextContent('Saved')

    // The unmount's final flush finds nothing left to write.
    view.unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(controls.save).toHaveBeenCalledTimes(1)
  })
})

describe('mounting hidden (PLA-513)', () => {
  it('starts suspended when the window is already hidden, and writes once it is visible', async () => {
    await setVisibility('hidden')
    await renderWithEditor()

    // The engine attached suspended: a revision is owed, but no timer may probe a dead
    // endpoint - an hour of hidden time writes nothing.
    typeInto('Original body, hidden mount')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_600_000)
    })
    expect(controls.save).not.toHaveBeenCalled()
    expect(screen.getByRole('status')).toHaveTextContent('Unsaved changes')

    // Visible again: the healthy debounce lands the write.
    await setVisibility('visible')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500)
    })
    expect(controls.save).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('status')).toHaveTextContent('Saved')
  })
})

describe('remount after a failed final flush (PLA-513)', () => {
  it('recovers the unconfirmed bytes, remembers the failure, and saves them automatically', async () => {
    const first = await renderWithEditor()
    typeInto('Original body, lost and found')
    // Unmount with the server down: the final flush starts, and it fails.
    controls.save.mockImplementation(() => Promise.reject(new Error('offline')))
    first.unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(controls.save).toHaveBeenCalledTimes(1)

    // The server is back. Remounting the same document reattaches to the same engine:
    // the failure is remembered (the last save genuinely failed), and the session is
    // still retained because bytes are still owed...
    controls.save.mockImplementation(() => Promise.resolve({ version: 2 }))
    const view = await renderWithEditor()
    expect(screen.getByRole('status')).toHaveTextContent('Could not save')
    expect(saveSessionEngine(controls.artifactId) !== null).toBe(true)

    // ...and the retry is owed: after the backoff, the unconfirmed bytes are written
    // automatically (no user input needed) and carries the confirmed version forward.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(retryDelay(1))
    })
    expect(controls.save).toHaveBeenCalledTimes(2)
    expect(controls.save).toHaveBeenLastCalledWith({
      content: 'Original body, lost and found',
      expected_version: 1,
    })
    expect(screen.getByRole('status')).toHaveTextContent('Saved')

    // Clean up so the next test is not contaminated.
    view.unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
  })

  it('reopens while the old write is still in flight and joins the existing writer', async () => {
    // Each write gets its own deferred promise so we control exactly when they settle.
    const writes: Array<{
      content: string
      resolve: (value: { version: number }) => void
    }> = []
    controls.save.mockImplementation(
      (req) =>
        new Promise((resolve) => {
          writes.push({ content: req.content, resolve: resolve })
        }),
    )

    const first = await renderWithEditor()
    typeInto('older pending edit')
    first.unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(controls.save).toHaveBeenCalledTimes(1)

    // Remount the same document: the session reattaches to the same engine. The editor
    // is seeded with the pending bytes, and typing the newest text coalesces into the
    // same pipeline - a second unmount does NOT start a competing write.
    const second = await renderWithEditor()
    typeInto('newest pending edit')
    second.unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(controls.save).toHaveBeenCalledTimes(1) // single-writer contract holds

    // Clean up: settle all in-flight writes so the gate clears for the next test.
    // Resolving the first write lets the drain start the second; resolve both rounds.
    for (const w of writes) {
      w.resolve({ version: 2 })
    }
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500)
    })
    for (const w of writes) {
      w.resolve({ version: 2 })
    }
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500)
    })
  })

  it('a late old success after reopening cannot erase a newer pending owner', async () => {
    // Each write gets its own deferred promise.
    const writes: Array<{
      content: string
      resolve: (value: { version: number }) => void
    }> = []
    controls.save.mockImplementation(
      (req) =>
        new Promise((resolve) => {
          writes.push({ content: req.content, resolve: resolve })
        }),
    )

    const first = await renderWithEditor()
    typeInto('older edit')
    first.unmount() // flush → write #1 'older edit' in flight
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(controls.save).toHaveBeenCalledTimes(1)

    // Remount: the engine still owes 'older edit'. The student types the newer text.
    const second = await renderWithEditor()
    typeInto('newer edit')
    // The gate is still blocked: a pending write stands.
    expect(() => assertUpdateSafe()).toThrow()

    // The old write settles successfully. The baseline moves to 'older edit', and the
    // retained engine writes the newest body (the drain drives it).
    writes[0].resolve({ version: 2 })
    await vi.advanceTimersByTimeAsync(0)
    expect(controls.save).toHaveBeenCalledTimes(2)
    expect(controls.save).toHaveBeenLastCalledWith({ content: 'newer edit', expected_version: 2 })

    // The newest body has landed: the gate is cleared.
    writes[1].resolve({ version: 3 })
    await vi.advanceTimersByTimeAsync(0)
    expect(() => assertUpdateSafe()).not.toThrow() // gate cleared: the newest body landed

    // Clean up.
    second.unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
  })

  it('a late old failure after reopening preserves recovery and the failure streak', async () => {
    // Each write gets its own deferred promise; the first fails, the second succeeds.
    const writes: Array<{
      content: string
      resolve: (value: { version: number }) => void
      reject: (error: Error) => void
    }> = []
    controls.save.mockImplementation(
      (req) =>
        new Promise((resolve, reject) => {
          writes.push({ content: req.content, resolve: resolve, reject: reject })
        }),
    )

    const first = await renderWithEditor()
    typeInto('older edit')
    first.unmount() // flush → write #1 'older edit' in flight
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(controls.save).toHaveBeenCalledTimes(1)

    // Remount and type the newer text.
    const second = await renderWithEditor()
    typeInto('newer edit')

    // The old write fails late: the failure applies to the retained engine. The drain
    // stops writing because a failure stands. The failure's backoff overrides the edit's
    // debounce (the engine's single timer slot), so the next write is on the backoff.
    writes[0].reject(new Error('offline'))
    await vi.advanceTimersByTimeAsync(0)
    // No immediate write: the drain broke, and the backoff hasn't fired yet.
    expect(controls.save).toHaveBeenCalledTimes(1)

    // The backoff fires: the newest body is written.
    await vi.advanceTimersByTimeAsync(retryDelay(1))
    expect(controls.save).toHaveBeenCalledTimes(2)
    expect(controls.save).toHaveBeenLastCalledWith({ content: 'newer edit', expected_version: 1 })

    // The newest body succeeds, clearing the gate.
    writes[1].resolve({ version: 3 })
    await vi.advanceTimersByTimeAsync(0)
    expect(() => assertUpdateSafe()).not.toThrow()

    // No fast retry: the failure streak was preserved. Advancing further produces no
    // more writes (the streak reset on success, and nothing is owed).
    await vi.advanceTimersByTimeAsync(retryDelay(1))
    expect(controls.save).toHaveBeenCalledTimes(2)
    expect(vi.getTimerCount()).toBe(0)

    // Clean up.
    second.unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
  })
})

it('retains recovered text when a reopened editor leaves before becoming ready', async () => {
  controls.save.mockRejectedValue(new Error('offline'))
  const first = await renderWithEditor()
  typeInto('Keep this recovered text')
  first.unmount()
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  const unopened = renderWorkspace()
  unopened.unmount()
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(
    controls.save.mock.calls.every(([body]) => body.content === 'Keep this recovered text'),
  ).toBe(true)
  controls.save.mockResolvedValue({ version: 2 })
  const recovered = await renderWithEditor()
  recovered.unmount()
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
})
