/**
 * PLA-494: the draft editor's asynchronous initialization and teardown.
 *
 * These tests inject deferred and rejected Crepe construction into the actual
 * `DraftEditor` component - not a detached helper - and hold it to the failure
 * contract: a rejected setup shows one concise retry/exit state instead of a blank
 * writing area, the supplied draft body survives untouched, no `onChange` or
 * `onEditorReady` fires for a failed or superseded editor (or for an editor that is
 * merely still being created), at most one attempt is live per mounted generation,
 * late resolution/rejection after unmount or replacement wires and reports nothing,
 * a rejecting `destroy()` never surfaces as an unhandled promise, and the ordinary
 * create/edit/unmount path keeps working.
 *
 * The fake models the pinned `@milkdown/core@7.22.0` lifecycle as read from its
 * bundle: `create()` moves the status to `OnCreate` and, on rejection, never moves
 * it back; `destroy()` while `OnCreate` reschedules itself every 50ms forever. The
 * component therefore must not destroy an editor stuck in `OnCreate` (a synchronous
 * fake could not feel that loop, so the fake keeps a bounded tick counter as the
 * evidence that no attempt was made), while `Idle` (create never ran) and `Created`
 * are safe to destroy. Each attempt also gets its own host element, so a late
 * create or destroy can never disturb the editor that replaced it.
 */
import { cleanup, act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createRef, StrictMode } from 'react'
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'

import DraftWorkspacePage from '@/app/classes/[id]/drafts/[artifactId]/page'
import { DraftEditor } from '@/components/drafts/draft-editor'
import type { DraftEditorHandle } from '@/components/drafts/draft-editor'
import { RouterProvider } from '@/router/hooks'

const BODY = 'Original body paragraph'

/** The injected Crepe stand-in's observable surface. */
type FakeCrepe = {
  root: HTMLElement
  defaultValue: string
  status: 'Idle' | 'OnCreate' | 'Created' | 'OnDestroy' | 'Destroyed'
  destroyed: boolean
  destroyCalls: number
  view: { dom: HTMLElement }
  events: {
    added: Array<[string, EventListenerOrEventListenerObject]>
    removed: Array<[string, EventListenerOrEventListenerObject]>
  }
  changeHandler: ((ctx: unknown, markdown: string, prev: string | null) => void) | null
}

type Pending = {
  instance: FakeCrepe
  resolve: (value?: unknown) => void
  reject: (reason?: unknown) => void
}

const h = vi.hoisted(() => ({
  instances: [] as unknown[],
  pending: [] as unknown[],
  createMode: 'resolve' as 'resolve' | 'reject' | 'defer',
  destroyMode: 'resolve' as 'resolve' | 'reject' | 'throw',
  addFeatureThrows: false,
  destroyRetries: 0,
}))

const kitUtils = vi.hoisted(() => ({
  getMarkdown: vi.fn(() => () => 'serialized markdown'),
  replaceAll: vi.fn((markdown: string) => () => ({ replaced: markdown })),
  markdownToSlice: vi.fn((markdown: string) => () => ({ sliceFor: markdown })),
}))

const plugins = vi.hoisted(() => ({
  commentHighlightsPlugin: vi.fn(),
  setComments: vi.fn(),
  jumpToComment: vi.fn(() => true),
  citationHighlightsPlugin: vi.fn(),
  writeSuggestionPlugin: vi.fn(),
}))

vi.mock('@milkdown/crepe/builder', () => {
  type Status = 'Idle' | 'OnCreate' | 'Created' | 'OnDestroy' | 'Destroyed'

  class FakeCrepe {
    root: HTMLElement
    defaultValue: string
    status: Status = 'Idle'
    destroyed = false
    destroyCalls = 0
    changeHandler: ((ctx: unknown, markdown: string, prev: string | null) => void) | null = null
    view: { dom: HTMLElement }
    events: {
      added: Array<[string, EventListenerOrEventListenerObject]>
      removed: Array<[string, EventListenerOrEventListenerObject]>
    } = { added: [], removed: [] }
    editor: {
      action: (fn: (ctx: { get: (_key: unknown) => unknown }) => unknown) => unknown
      status: Status
    }

    constructor(options: { root: HTMLElement; defaultValue: string }) {
      this.root = options.root
      this.defaultValue = options.defaultValue
      const dom = document.createElement('div')
      const events = this.events
      const realAdd = dom.addEventListener.bind(dom)
      const realRemove = dom.removeEventListener.bind(dom)
      dom.addEventListener = ((type: string, fn: unknown, opts?: unknown) => {
        events.added.push([type, fn as EventListenerOrEventListenerObject])
        ;(realAdd as (t: string, f: unknown, o: unknown) => void)(type, fn, opts)
      }) as typeof dom.addEventListener
      dom.removeEventListener = ((type: string, fn: unknown, opts?: unknown) => {
        events.removed.push([type, fn as EventListenerOrEventListenerObject])
        ;(realRemove as (t: string, f: unknown, o: unknown) => void)(type, fn, opts)
      }) as typeof dom.removeEventListener
      this.view = { dom }
      // Mirrors the real Editor's public `status` getter: the value as of now.
      const editor: {
        action: (fn: (ctx: { get: (_key: unknown) => unknown }) => unknown) => unknown
        status: Status
      } = {
        action: (fn) => fn({ get: () => this.view }),
        status: this.status,
      }
      Object.defineProperty(editor, 'status', {
        get: () => this.status,
        enumerable: true,
      })
      this.editor = editor
      h.instances.push(this)
    }

    addFeature() {
      if (h.addFeatureThrows) throw new Error('feature setup failed synchronously')
      return this
    }

    on(register: (api: { markdownUpdated: (fn: unknown) => void }) => void) {
      register({
        markdownUpdated: (fn: unknown) => {
          this.changeHandler = fn as (ctx: unknown, markdown: string, prev: string | null) => void
        },
      })
      return this
    }

    create(): Promise<void> {
      // As in @milkdown/core 7.22.0: OnCreate first, plugins after; a rejection
      // never moves the status back out of OnCreate.
      this.status = 'OnCreate'
      if (h.createMode === 'reject') return Promise.reject(new Error('Crepe construction failed'))
      if (h.createMode === 'defer') {
        return new Promise<void>((resolve, reject) => {
          h.pending.push({
            instance: this,
            resolve: () => {
              this.status = 'Created'
              resolve()
            },
            reject,
          })
        })
      }
      this.status = 'Created'
      return Promise.resolve()
    }

    destroy(): Promise<void> {
      this.destroyCalls += 1
      if (this.status === 'OnCreate') {
        // The upstream retry loop, faithfully but bounded so a regression leaves
        // evidence (a tick counter) rather than a runaway test process.
        h.destroyRetries += 1
        const timer = setTimeout(() => {
          if (this.status === 'OnCreate') h.destroyRetries += 1
        }, 50)
        const unref = (timer as unknown as { unref?: () => void }).unref
        if (typeof unref === 'function') unref.call(timer)
        return Promise.resolve()
      }
      this.destroyed = true
      this.root.innerHTML = ''
      this.status = 'Destroyed'
      if (h.destroyMode === 'reject') return Promise.reject(new Error('destroy failed'))
      if (h.destroyMode === 'throw') throw new Error('destroy threw synchronously')
      return Promise.resolve()
    }
  }
  return { CrepeBuilder: FakeCrepe }
})

vi.mock('@milkdown/crepe/feature/toolbar', () => ({ toolbar: () => {} }))
vi.mock('@milkdown/crepe/feature/block-edit', () => ({ blockEdit: () => {} }))
vi.mock('@milkdown/crepe/feature/code-mirror', () => ({ codeMirror: () => {} }))
vi.mock('@milkdown/crepe/feature/cursor', () => ({ cursor: () => {} }))
vi.mock('@milkdown/crepe/feature/latex', () => ({ latex: () => {} }))
vi.mock('@milkdown/crepe/feature/link-tooltip', () => ({ linkTooltip: () => {} }))
vi.mock('@milkdown/crepe/feature/list-item', () => ({ listItem: () => {} }))
vi.mock('@milkdown/crepe/feature/placeholder', () => ({ placeholder: () => {} }))
vi.mock('@milkdown/crepe/feature/table', () => ({ table: () => {} }))
vi.mock('@milkdown/kit/core', () => ({
  editorViewCtx: 'editor-view-ctx',
  EditorStatus: {
    Idle: 'Idle',
    OnCreate: 'OnCreate',
    Created: 'Created',
    OnDestroy: 'OnDestroy',
    Destroyed: 'Destroyed',
  },
}))
vi.mock('@milkdown/kit/utils', () => kitUtils)
vi.mock('@/components/drafts/comment-highlights', () => ({
  commentHighlightsPlugin: plugins.commentHighlightsPlugin,
  setComments: plugins.setComments,
  jumpToComment: plugins.jumpToComment,
}))
vi.mock('@/components/drafts/citation-highlights', () => ({
  citationHighlightsPlugin: plugins.citationHighlightsPlugin,
}))
vi.mock('@/components/drafts/write-suggestion', () => ({
  writeSuggestionPlugin: plugins.writeSuggestionPlugin,
}))

// The workspace-page half of the file: the route lazy-loads this component, and the
// exit wiring is asserted through the same seam the route uses.
vi.mock('@/router/hooks', async (original) => ({
  ...(await original<object>()),
  useParams: () => ({ id: '1', artifactId: '7' }),
}))
vi.mock('@/router/dynamic', () => ({
  default: () =>
    function EditorStub(props: { initialMarkdown: string; onExit?: () => void }) {
      return (
        <button type="button" onClick={() => props.onExit?.()}>
          Mock editor holding {JSON.stringify(props.initialMarkdown)}
        </button>
      )
    },
}))
vi.mock('@/components/layout/page-chrome', () => ({
  useFullBleed: () => {},
  useImmersiveChrome: () => {},
  HeaderCrumb: () => null,
}))
vi.mock('@/lib/hooks/use-media-query', () => ({ useMediaQuery: () => false }))
vi.mock('@/lib/hooks/use-classes', () => ({
  useClasses: () => ({ data: [{ id: 1, name: 'History' }] }),
}))
// Plain closures rather than vi.fn: vitest's `restoreMocks` strips mocked
// implementations between tests, and this harness's shape must survive to the last one.
vi.mock('@/lib/hooks/use-drafts', async (original) => ({
  ...(await original<object>()),
  useDraft: () => ({
    data: {
      id: 7,
      class_id: 1,
      part_id: 8,
      title: 'My essay',
      body: BODY,
      body_version: 1,
      state: 'ready',
      stage_detail: null,
      error_message: null,
    },
    isError: false,
    isPending: false,
    isFetching: false,
    refetch: () => {},
  }),
  useDraftStatus: () => ({ data: { state: 'ready' } }),
  usePendingEdit: () => ({ data: null }),
  useComments: () => ({ data: [] }),
  useWriterSessions: () => ({ data: [] }),
  useExportAvailability: () => ({ data: { available: false } }),
  useLiveDraftSuggestion: () => ({ data: null }),
  useStartPass: () => ({ mutateAsync: async () => ({}), isPending: false }),
  useStartReview: () => ({ mutateAsync: async () => ({}), isPending: false }),
  useCancelDraftRun: () => ({ mutateAsync: async () => ({}), isPending: false }),
  useRenameDraft: () => ({ mutate: () => {}, isPending: false }),
  useUpdateBody: () => ({
    mutateAsync: async () => ({ version: 2 }),
    isPending: false,
  }),
}))
vi.mock('@/lib/runtime', async (original) => ({
  ...(await original<object>()),
  printCurrentDocument: async () => undefined,
}))
vi.mock('@/components/drafts/brief-card', () => ({ BriefCard: () => null }))
vi.mock('@/components/drafts/source-ledger', () => ({ SourceLedger: () => null }))
vi.mock('@/components/drafts/plan-panel', () => ({ PlanPanel: () => null }))
vi.mock('@/components/drafts/comment-list', () => ({ CommentList: () => null }))
vi.mock('@/components/chat/chat-pane', () => ({ ChatPane: () => null }))
vi.mock('@/components/solutions/revision-history', () => ({ RevisionHistory: () => null }))

function crepes(): FakeCrepe[] {
  return h.instances as FakeCrepe[]
}

function pendingFor(instance: FakeCrepe): Pending {
  const entry = (h.pending as Pending[]).find((p) => p.instance === instance)
  if (!entry) throw new Error('No deferred create is pending for this editor')
  return entry
}

/**
 * Let queued microtasks and Node's unhandled-rejection detection settle. A span
 * above 50ms also gives any upstream-style destroy-retry timer a chance to tick.
 */
async function flush(withinMs = 0) {
  await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, withinMs))
    await new Promise<void>((resolve) => setTimeout(resolve, 0))
  })
}

function watchUnhandledRejections() {
  const seen: unknown[] = []
  const onRejection = (reason: unknown) => seen.push(reason)
  process.on('unhandledRejection', onRejection)
  return { seen, stop: () => process.off('unhandledRejection', onRejection) }
}

function renderEditor(overrides: { onExit?: (() => void) | undefined } = {}) {
  const ref = createRef<DraftEditorHandle>()
  const handlers = {
    onChange: vi.fn(),
    onEditorReady: vi.fn(),
    onExit: vi.fn(),
    ...overrides,
  }
  render(<DraftEditor ref={ref} initialMarkdown={BODY} {...handlers} />)
  return { ref, ...handlers }
}

beforeEach(() => {
  h.instances.length = 0
  h.pending.length = 0
  h.createMode = 'resolve'
  h.destroyMode = 'resolve'
  h.addFeatureThrows = false
  h.destroyRetries = 0
  // Re-arm the hoisted spies: `restoreMocks` may have stripped their implementations
  // after the previous test.
  kitUtils.getMarkdown.mockReset().mockImplementation(() => () => 'serialized markdown')
  kitUtils.replaceAll
    .mockReset()
    .mockImplementation((markdown: string) => () => ({ replaced: markdown }))
  kitUtils.markdownToSlice
    .mockReset()
    .mockImplementation((markdown: string) => () => ({ sliceFor: markdown }))
  plugins.setComments.mockReset()
  plugins.jumpToComment.mockReset().mockImplementation(() => true)
  plugins.commentHighlightsPlugin.mockClear()
  plugins.citationHighlightsPlugin.mockClear()
  plugins.writeSuggestionPlugin.mockClear()
})

afterEach(() => {
  cleanup()
  window.history.replaceState({}, '', '/')
})

describe('DraftEditor initialization recovery', () => {
  it('shows a retry/exit state when creation rejects, and reports nothing about the draft', async () => {
    const unhandled = watchUnhandledRejections()
    h.createMode = 'reject'
    const { ref, onChange, onEditorReady } = renderEditor()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Could not open the editor')
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Exit to class' })).toBeInTheDocument()

    // No false-ready, no false-save: the workspace hears nothing at all.
    expect(onEditorReady).not.toHaveBeenCalled()
    expect(onChange).not.toHaveBeenCalled()
    // The imperative handle says "no editor" rather than pretending.
    expect(ref.current?.markdown()).toBeNull()
    expect(ref.current?.view()).toBeNull()
    expect(ref.current?.toSlice('anything')).toBeNull()
    expect(ref.current?.jumpToComment(3)).toBe(false)
    expect(() => ref.current?.reset('anything')).not.toThrow()
    expect(() => ref.current?.setComments([])).not.toThrow()

    // Not a blank writing area: the failed host is out of the way, not kept as the surface.
    expect(document.querySelector('.draft-editor')).not.toBeVisible()
    const [failed] = crepes()
    // Milkdown leaves a rejected create stuck in `OnCreate`, where destroy is the
    // upstream 50ms retry loop - so no destroy may even be attempted on it. Its host
    // is still isolated out of the document.
    expect(failed.status).toBe('OnCreate')
    expect(failed.destroyCalls).toBe(0)
    expect(failed.root.isConnected).toBe(false)
    expect(document.querySelector('.draft-editor')?.childElementCount).toBe(0)
    // Even a stray document event from the failed editor reports nothing.
    failed.changeHandler?.(null, 'ghost edit', 'earlier')
    expect(onChange).not.toHaveBeenCalled()

    await flush(80)
    expect(h.destroyRetries).toBe(0)
    expect(unhandled.seen).toEqual([])
    unhandled.stop()
  })

  it('does not render an exit control the workspace never wired', async () => {
    h.createMode = 'reject'
    renderEditor({ onExit: undefined })
    await screen.findByRole('alert')
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /exit/i })).not.toBeInTheDocument()
  })

  it('retry reseeds the supplied body and keeps exactly one live editor', async () => {
    h.createMode = 'reject'
    const { onChange, onEditorReady } = renderEditor()
    await screen.findByRole('alert')

    h.createMode = 'resolve'
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(onEditorReady).toHaveBeenCalledTimes(1))

    const [first, second] = crepes()
    // The body the workspace supplied is what the retry seeds - not an empty document.
    expect(second.defaultValue).toBe(BODY)
    // The rejected first attempt is stuck upstream (undestroyable without the retry
    // loop), but it is gone from the document: each attempt owns its own host.
    expect(first.status).toBe('OnCreate')
    expect(first.destroyCalls).toBe(0)
    expect(first.root.isConnected).toBe(false)
    expect(second.destroyed).toBe(false)
    expect(second.root.isConnected).toBe(true)
    expect(document.querySelector('.draft-editor')?.childElementCount).toBe(1)
    expect(onEditorReady).toHaveBeenCalledWith(second.view)
    expect(onChange).not.toHaveBeenCalled()
    expect(document.querySelector('.draft-editor')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    await flush(80)
    expect(h.destroyRetries).toBe(0)
  })

  it('rapid repeated retries leave at most one live attempt', async () => {
    const unhandled = watchUnhandledRejections()
    h.createMode = 'reject'
    const { onEditorReady } = renderEditor()
    await screen.findByRole('alert')

    h.createMode = 'defer'
    const button = screen.getByRole('button', { name: 'Retry' })
    // Two synchronous activations before any re-render: each retry retires the
    // attempt before it, so only the newest can ever be live.
    act(() => {
      button.click()
      button.click()
    })
    const [first, second, third] = crepes()
    expect(crepes()).toHaveLength(3)
    // At most one attempt may ever be live, and retired attempts are out of the
    // document: hosts are isolated per attempt.
    expect(first.root.isConnected).toBe(false)
    expect(second.root.isConnected).toBe(false)
    expect(third.root.isConnected).toBe(true)
    expect(third.destroyed).toBe(false)

    // The replaced second attempt's create only now succeeds: a retired attempt
    // disposes itself once its create settles (deferred teardown), wiring nothing.
    pendingFor(second).resolve()
    await flush()
    expect(second.destroyed).toBe(true)
    expect(second.events.added).toEqual([])
    expect(third.destroyed).toBe(false)

    pendingFor(third).resolve()
    await waitFor(() => expect(onEditorReady).toHaveBeenCalledTimes(1))
    expect(third.events.added.map(([type]) => type)).toEqual(['click', 'keydown'])
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    await flush(80)
    expect(h.destroyRetries).toBe(0)
    expect(unhandled.seen).toEqual([])
    unhandled.stop()
  })

  it('a create that resolves after unmount wires and reports nothing', async () => {
    const unhandled = watchUnhandledRejections()
    h.createMode = 'defer'
    const { ref, onChange, onEditorReady } = renderEditor()
    const crepe = crepes()[0]

    // Teardown while the create is still `OnCreate` defers destroy - destroying
    // there would be the upstream retry loop - but the host is already detached.
    cleanup()
    expect(crepe.destroyCalls).toBe(0)
    expect(crepe.root.isConnected).toBe(false)

    // Once the create settles, the deferred teardown runs.
    pendingFor(crepe).resolve()
    await flush()
    expect(crepe.destroyed).toBe(true)
    expect(onEditorReady).not.toHaveBeenCalled()
    expect(onChange).not.toHaveBeenCalled()
    expect(crepe.events.added).toEqual([])
    expect(ref.current).toBeNull()
    await flush(80)
    expect(h.destroyRetries).toBe(0)
    expect(unhandled.seen).toEqual([])
    unhandled.stop()
  })

  it('a create that rejects after unmount leaves no unhandled rejection and no retry loop', async () => {
    const unhandled = watchUnhandledRejections()
    h.createMode = 'defer'
    const { onChange, onEditorReady } = renderEditor()
    const crepe = crepes()[0]

    cleanup()
    pendingFor(crepe).reject(new Error('late failure'))
    await flush(80)
    // Stuck in `OnCreate`: the component abandons the attempt without ever
    // attempting the destroy that would spin.
    expect(crepe.destroyCalls).toBe(0)
    expect(h.destroyRetries).toBe(0)
    expect(onEditorReady).not.toHaveBeenCalled()
    expect(onChange).not.toHaveBeenCalled()
    expect(unhandled.seen).toEqual([])
    unhandled.stop()
  })

  it('a late-resolving old attempt cannot ready or attach over the remounted editor', async () => {
    const unhandled = watchUnhandledRejections()
    h.createMode = 'defer'
    renderEditor()
    const oldCrepe = crepes()[0]
    cleanup()

    // A new mounted generation: the same draft, a fresh editor lifecycle.
    h.createMode = 'resolve'
    const fresh = renderEditor()
    await waitFor(() => expect(fresh.onEditorReady).toHaveBeenCalledTimes(1))
    const freshCrepe = crepes()[1]

    // The old attempt's creation only now succeeds - into a window that moved on.
    pendingFor(oldCrepe).resolve()
    await flush()

    expect(fresh.onEditorReady).toHaveBeenCalledTimes(1)
    expect(oldCrepe.events.added).toEqual([])
    expect(oldCrepe.destroyed).toBe(true)
    // The generations never share DOM: the old host was detached when its
    // generation ended, and the late create could only write into that.
    expect(oldCrepe.root.isConnected).toBe(false)
    expect(freshCrepe.root.isConnected).toBe(true)
    expect(freshCrepe.events.added.map(([type]) => type)).toEqual(['click', 'keydown'])

    // Stale events from the old editor reach the workspace as nothing; the new
    // editor's own change still flows.
    oldCrepe.changeHandler?.(null, 'stale words', 'before')
    expect(fresh.onChange).not.toHaveBeenCalled()
    freshCrepe.changeHandler?.(null, 'fresh words', 'before')
    expect(fresh.onChange).toHaveBeenCalledWith('fresh words')
    await flush(80)
    expect(h.destroyRetries).toBe(0)
    expect(unhandled.seen).toEqual([])
    unhandled.stop()
  })

  it('unmount removes exactly the listeners the editor attached', async () => {
    const { onEditorReady } = renderEditor()
    await waitFor(() => expect(onEditorReady).toHaveBeenCalledTimes(1))
    const crepe = crepes()[0]
    expect(crepe.events.added.map(([type]) => type)).toEqual(['click', 'keydown'])

    cleanup()
    expect(crepe.events.removed).toEqual(crepe.events.added)
    expect(crepe.destroyed).toBe(true)
  })

  it('a destroy that rejects on teardown is never an unhandled promise', async () => {
    const unhandled = watchUnhandledRejections()
    const { onEditorReady } = renderEditor()
    await waitFor(() => expect(onEditorReady).toHaveBeenCalledTimes(1))
    const crepe = crepes()[0]

    h.destroyMode = 'reject'
    cleanup()
    await flush()
    expect(crepe.destroyed).toBe(true)
    expect(unhandled.seen).toEqual([])
    unhandled.stop()
  })

  it('a rejected create is never destroyed, so a hostile destroy is unreachable there too', async () => {
    const unhandled = watchUnhandledRejections()
    h.createMode = 'reject'
    h.destroyMode = 'throw'
    const { onEditorReady } = renderEditor()
    await screen.findByRole('alert')
    // A create that rejected is stuck in `OnCreate`; the destroying call that would
    // throw - or, in the real editor, spin at 50ms intervals - is never attempted.
    expect(crepes()[0].destroyCalls).toBe(0)
    await flush(80)
    expect(h.destroyRetries).toBe(0)
    expect(unhandled.seen).toEqual([])

    h.createMode = 'resolve'
    h.destroyMode = 'resolve'
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(onEditorReady).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    unhandled.stop()
  })

  it('a synchronously throwing feature still releases the constructed editor', async () => {
    const unhandled = watchUnhandledRejections()
    // A feature throws inside `addFeature`, before `create()` ever runs. The editor
    // was recorded before configuration, so it is destroyable (`Idle`) and is
    // destroyed - the reference is not lost just because the chain threw.
    h.addFeatureThrows = true
    h.destroyMode = 'throw'
    const { onEditorReady } = renderEditor()
    await screen.findByRole('alert')
    const [instance] = crepes()
    // Destroy went down the safe path (`Idle` editor, create never ran) and ran
    // before its simulated throw; the component swallowed that without a false retry.
    expect(instance.destroyCalls).toBe(1)
    expect(instance.destroyed).toBe(true)
    expect(instance.root.isConnected).toBe(false)
    expect(onEditorReady).not.toHaveBeenCalled()
    await flush(80)
    expect(h.destroyRetries).toBe(0)
    expect(unhandled.seen).toEqual([])

    h.addFeatureThrows = false
    h.destroyMode = 'resolve'
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(onEditorReady).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    unhandled.stop()
  })

  it('reports nothing for an editor that updates mid-creation and then rejects, and only live edits after retry', async () => {
    h.createMode = 'defer'
    const { onChange, onEditorReady } = renderEditor()
    const crepe = crepes()[0]
    // Crepe's `on` handlers are live before `create()` resolves: setup emits a
    // document update for an editor that has not opened yet.
    crepe.changeHandler?.(null, 'half-built document', 'seed')
    expect(onChange).not.toHaveBeenCalled()

    pendingFor(crepe).reject(new Error('create failed'))
    await screen.findByRole('alert')
    expect(onEditorReady).not.toHaveBeenCalled()
    expect(onChange).not.toHaveBeenCalled()

    h.createMode = 'defer'
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    const retry = crepes()[1]
    retry.changeHandler?.(null, 'premature emit', 'seed')
    expect(onChange).not.toHaveBeenCalled()
    pendingFor(retry).resolve()
    await waitFor(() => expect(onEditorReady).toHaveBeenCalledTimes(1))
    retry.changeHandler?.(null, 'real edit', 'seed')
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledWith('real edit')
  })

  it('survives a StrictMode effect remount while initialization is still in flight', async () => {
    const unhandled = watchUnhandledRejections()
    h.createMode = 'defer'
    const onEditorReady = vi.fn()
    render(
      <StrictMode>
        <DraftEditor initialMarkdown={BODY} onChange={vi.fn()} onEditorReady={onEditorReady} />
      </StrictMode>,
    )
    // React's dev-mode StrictMode runs mount -> cleanup -> mount while the first
    // create is still pending: the two attempts must be fully isolated.
    await waitFor(() => expect(crepes()).toHaveLength(2))
    const [first, second] = crepes()
    expect(first.root.isConnected).toBe(false)
    expect(second.root.isConnected).toBe(true)
    expect(document.querySelectorAll('.draft-editor > div')).toHaveLength(1)

    pendingFor(first).resolve()
    await flush()
    // Deferred teardown: the disposed attempt disposes itself once its create settles,
    // without wiring anything and without touching the editor that replaced it.
    expect(first.destroyed).toBe(true)
    expect(first.events.added).toEqual([])
    expect(onEditorReady).not.toHaveBeenCalled()

    pendingFor(second).resolve()
    await waitFor(() => expect(onEditorReady).toHaveBeenCalledTimes(1))
    expect(second.events.added.map(([type]) => type)).toEqual(['click', 'keydown'])

    cleanup()
    expect(second.destroyed).toBe(true)
    await flush(80)
    expect(h.destroyRetries).toBe(0)
    expect(unhandled.seen).toEqual([])
    unhandled.stop()
  })

  it('creates, edits, and unmounts the ordinary way', async () => {
    const unhandled = watchUnhandledRejections()
    const { ref, onChange, onEditorReady } = renderEditor()
    await waitFor(() => expect(onEditorReady).toHaveBeenCalledTimes(1))
    const crepe = crepes()[0]

    expect(crepe.defaultValue).toBe(BODY)
    expect(onEditorReady).toHaveBeenCalledWith(crepe.view)

    // The seed document's first report is not an edit; a real edit is.
    crepe.changeHandler?.(null, BODY, null)
    expect(onChange).not.toHaveBeenCalled()
    crepe.changeHandler?.(null, 'Edited paragraph', BODY)
    expect(onChange).toHaveBeenCalledWith('Edited paragraph')

    // The imperative handle serves the live editor.
    expect(ref.current?.markdown()).toBe('serialized markdown')
    expect(ref.current?.view()).toBe(crepe.view)
    expect(ref.current?.toSlice('a table')).toEqual({ sliceFor: 'a table' })
    const threads = [{ id: 5, quote: 'a phrase', severity: null }]
    ref.current?.setComments(threads)
    expect(plugins.setComments).toHaveBeenCalledWith(crepe.view, threads)
    expect(ref.current?.jumpToComment(4)).toBe(true)
    expect(plugins.jumpToComment).toHaveBeenCalledWith(crepe.view, 4)

    cleanup()
    expect(crepe.destroyed).toBe(true)
    await flush()
    expect(unhandled.seen).toEqual([])
    unhandled.stop()
  })
})

describe('draft workspace exit wiring', () => {
  function renderWorkspace() {
    window.history.replaceState({}, '', '/#/classes/1/drafts/7')
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <QueryClientProvider client={client}>
        <RouterProvider>
          <DraftWorkspacePage />
        </RouterProvider>
      </QueryClientProvider>,
    )
  }

  it('the failed editor exits to the class workspace that lists the draft', async () => {
    renderWorkspace()
    // The route's lazy editor carries the workspace's real navigation as its exit.
    const exit = await screen.findByRole('button', { name: /Mock editor holding/ })
    expect(exit).toHaveTextContent(JSON.stringify(BODY))
    await userEvent.click(exit)
    expect(window.location.hash).toBe('#/classes/1')
  })
})
