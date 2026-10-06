import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import DraftWorkspacePage from '@/app/classes/[id]/drafts/[artifactId]/page'
import { api, DraftBodyConflictError } from '@/lib/api'
import { saveSessionEngine } from '@/lib/drafts/save-session'
import { draftKeys, useDraft, useUpdateBody } from '@/lib/hooks/use-drafts'
import { RouterProvider, usePathname, useRouter } from '@/router/hooks'
import type { DraftBodySaved, DraftDetail } from '@/types'

// Keep the actual workspace, query hooks, save session and save engine. Only the
// editor widget and unrelated tools are replaced, so navigation exercises the
// same cached-body seeding path as a real draft-to-chat-to-draft visit.
vi.mock('@/router/dynamic', async () => {
  const { forwardRef, useEffect, useImperativeHandle, useRef, useState } = await import('react')
  const Editor = forwardRef<
    { reset: (body: string) => void; setComments: () => void },
    {
      initialMarkdown: string
      onChange: (body: string) => void
      onEditorReady: (view: unknown) => void
    }
  >(function Editor(props, ref) {
    const [body, setBody] = useState(props.initialMarkdown)
    const dom = useRef<HTMLTextAreaElement>(null)
    const ready = useRef(props.onEditorReady)
    useImperativeHandle(ref, () => ({ reset: setBody, setComments: () => undefined }))
    useEffect(() => {
      ready.current({ dom: dom.current })
    }, [])
    return (
      <textarea
        ref={dom}
        value={body}
        onChange={(event) => {
          setBody(event.target.value)
          props.onChange(event.target.value)
        }}
      />
    )
  })
  return { default: () => Editor }
})
vi.mock('@/components/layout/page-chrome', () => ({
  useFullBleed: vi.fn(),
  useImmersiveChrome: vi.fn(),
  HeaderCrumb: () => null,
}))
vi.mock('@/lib/hooks/use-media-query', () => ({ useMediaQuery: () => false }))
vi.mock('@/lib/hooks/use-classes', () => ({
  useClasses: () => ({ data: [{ id: 1, name: 'Class' }] }),
}))
vi.mock('@/lib/hooks/use-drafts', async (original) => ({
  ...(await original<object>()),
  useDraftStatus: () => ({ data: { state: 'ready' } }),
  usePendingEdit: () => ({ data: null }),
  useComments: () => ({ data: [] }),
  useWriterSessions: () => ({ data: [] }),
  useExportAvailability: () => ({ data: { available: false } }),
  useLiveDraftSuggestion: () => ({ data: null }),
}))
vi.mock('@/components/drafts/brief-card', () => ({ BriefCard: () => null }))
vi.mock('@/components/drafts/source-ledger', () => ({ SourceLedger: () => null }))
vi.mock('@/components/drafts/plan-panel', () => ({ PlanPanel: () => null }))
vi.mock('@/components/drafts/comment-list', () => ({ CommentList: () => null }))
vi.mock('@/components/chat/chat-pane', () => ({ ChatPane: () => null }))
vi.mock('@/components/solutions/revision-history', () => ({ RevisionHistory: () => null }))

let nextId = 9000
let draft: DraftDetail
let client: QueryClient

function Wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={client}>
      <RouterProvider>{children}</RouterProvider>
    </QueryClientProvider>
  )
}

function Routes() {
  const pathname = usePathname()
  const router = useRouter()
  return (
    <>
      <button onClick={() => router.push('/classes/1/chat')}>Long chat</button>
      <button onClick={() => router.push(`/classes/1/drafts/${draft.id}`)}>Reopen draft</button>
      {pathname.includes('/drafts/') ? <DraftWorkspacePage /> : <p>Long conversation</p>}
    </>
  )
}

beforeEach(() => {
  vi.useFakeTimers()
  draft = {
    id: ++nextId,
    class_id: 1,
    part_id: nextId + 100,
    kind: 'draft',
    title: 'Draft',
    body: 'Original body',
    body_version: 1,
    state: 'ready',
    stage_detail: null,
    problems_total: null,
    problems_done: 0,
    error_message: null,
    pending: false,
    created_at: '2026-10-05T00:00:00Z',
    updated_at: '2026-10-05T00:00:00Z',
  }
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 5000 } } })
  client.setQueryData(draftKeys.detail(draft.id), { ...draft })
  window.history.replaceState({}, '', `/#/classes/1/drafts/${draft.id}`)
  vi.spyOn(api, 'getDraft').mockImplementation(async () => ({ ...draft }))
  vi.spyOn(api, 'updateDraftBody').mockImplementation(async (_id, body) => {
    if (body.expected_version !== draft.body_version)
      throw new DraftBodyConflictError(409, {
        code: 'stale_body_version',
        current_version: draft.body_version,
        server_body: draft.body,
        detail: 'This draft changed somewhere else.',
      })
    draft = { ...draft, body: body.content, body_version: draft.body_version + 1 }
    return { saved: true, part_id: draft.part_id, version: draft.body_version }
  })
  localStorage.clear()
  Element.prototype.scrollIntoView = vi.fn()
})

afterEach(async () => {
  await act(async () => cleanup())
  client.clear()
  vi.useRealTimers()
})

async function advance(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

it('reopens its own confirmed writing immediately and saves the next edit without a false conflict', async () => {
  client.removeQueries({ queryKey: draftKeys.detail(draft.id) })
  render(<Routes />, { wrapper: Wrapper })
  await advance(1)
  expect(api.getDraft).toHaveBeenCalledOnce()
  const saved = 'Original body\n\nNative review saved note.'
  fireEvent.change(screen.getByRole('textbox', { name: 'Draft document' }), {
    target: { value: saved },
  })
  await advance(1501)
  expect(draft.body).toBe(saved)
  expect(screen.getByRole('status')).toHaveTextContent('Saved')

  fireEvent.click(screen.getByRole('button', { name: 'Long chat' }))
  await advance()
  expect(saveSessionEngine(String(draft.id))).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Reopen draft' }))
  await advance()
  expect(api.getDraft).toHaveBeenCalledOnce()
  const editor = screen.getByRole('textbox', { name: 'Draft document' })
  expect(editor).toHaveValue(saved)
  fireEvent.change(editor, { target: { value: saved + '\n\nPaste persistence probe.' } })
  await advance(1501)
  expect(draft.body).toContain('Paste persistence probe.')
  expect(api.updateDraftBody).toHaveBeenLastCalledWith(
    draft.id,
    expect.objectContaining({ expected_version: 2 }),
  )
  expect(screen.getByRole('status')).toHaveTextContent('Saved')
  expect(screen.queryByText('Changed elsewhere')).not.toBeInTheDocument()
})

it('publishes the final unmount flush before a later mount recreates the save session', async () => {
  render(<Routes />, { wrapper: Wrapper })
  await advance()
  const saved = 'Typed immediately before navigation'
  fireEvent.change(screen.getByRole('textbox', { name: 'Draft document' }), {
    target: { value: saved },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Long chat' }))
  await advance()
  expect(saveSessionEngine(String(draft.id))).toBeNull()
  expect(draft.body).toBe(saved)
  fireEvent.click(screen.getByRole('button', { name: 'Reopen draft' }))
  await advance()
  expect(screen.getByRole('textbox', { name: 'Draft document' })).toHaveValue(saved)
})

it('reopens confirmed writing when a lost acknowledgement is recovered by the version guard', async () => {
  const write = vi.mocked(api.updateDraftBody).getMockImplementation()!
  vi.mocked(api.updateDraftBody).mockImplementationOnce(async (id, body) => {
    await write(id, body)
    throw new Error('The save response was lost')
  })
  render(<Routes />, { wrapper: Wrapper })
  await advance()
  const saved = 'Writing whose acknowledgement was lost'
  fireEvent.change(screen.getByRole('textbox', { name: 'Draft document' }), {
    target: { value: saved },
  })
  await advance(3502)
  expect(draft.body).toBe(saved)
  expect(screen.getByRole('status')).toHaveTextContent('Saved')
  fireEvent.click(screen.getByRole('button', { name: 'Long chat' }))
  await advance()
  expect(saveSessionEngine(String(draft.id))).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Reopen draft' }))
  await advance()
  expect(screen.getByRole('textbox', { name: 'Draft document' })).toHaveValue(saved)
})

it('does not let a pre-save detail read replace the acknowledged body', async () => {
  let finishRead!: (value: DraftDetail) => void
  const old = { ...draft }
  vi.mocked(api.getDraft).mockReturnValue(
    new Promise((resolve) => {
      finishRead = resolve
    }),
  )
  const read = client
    .fetchQuery({
      queryKey: draftKeys.detail(draft.id),
      staleTime: 0,
      queryFn: ({ signal }) => api.getDraft(draft.id, signal),
    })
    .catch(() => undefined)
  const { result } = renderHook(() => useUpdateBody(draft.id), { wrapper: Wrapper })
  await act(async () => {
    await result.current.mutateAsync({ content: 'Confirmed writing', expected_version: 1 })
  })
  await act(async () => {
    finishRead(old)
    await read
  })
  expect(client.getQueryData<DraftDetail>(draftKeys.detail(draft.id))).toMatchObject({
    body: 'Confirmed writing',
    body_version: 2,
  })
})

it('does not roll a newer authoritative body back when an older save acknowledgement arrives', async () => {
  let finishSave!: (value: DraftBodySaved) => void
  vi.mocked(api.updateDraftBody).mockReturnValue(
    new Promise((resolve) => {
      finishSave = resolve
    }),
  )
  const oldRead = client
    .fetchQuery({
      queryKey: draftKeys.detail(draft.id),
      staleTime: 0,
      queryFn: () => new Promise<DraftDetail>(() => {}),
    })
    .catch(() => undefined)
  const { result } = renderHook(() => useUpdateBody(draft.id), { wrapper: Wrapper })
  let save!: Promise<unknown>
  await act(async () => {
    save = result.current.mutateAsync({ content: 'Version two', expected_version: 1 })
  })
  client.setQueryData(draftKeys.detail(draft.id), {
    ...draft,
    body: 'Version three',
    body_version: 3,
  })
  await act(async () => {
    finishSave({ saved: true, part_id: draft.part_id, version: 2 })
    await save
  })
  await oldRead
  expect(client.getQueryData<DraftDetail>(draftKeys.detail(draft.id))).toMatchObject({
    body: 'Version three',
    body_version: 3,
  })
})

it('keeps newer typing in the editor while an earlier save updates the detail cache', async () => {
  const write = vi.mocked(api.updateDraftBody).getMockImplementation()!
  let finishFirst!: () => void
  vi.mocked(api.updateDraftBody).mockImplementationOnce(
    (id, body) =>
      new Promise((resolve) => {
        finishFirst = () => {
          void write(id, body).then(resolve)
        }
      }),
  )
  render(<Routes />, { wrapper: Wrapper })
  await advance()
  const editor = screen.getByRole('textbox', { name: 'Draft document' })
  fireEvent.change(editor, { target: { value: 'Earlier edit' } })
  await advance(1501)
  fireEvent.change(editor, { target: { value: 'Newer typing' } })
  await act(async () => finishFirst())
  expect(editor).toHaveValue('Newer typing')
  await advance(1501)
  expect(draft.body).toBe('Newer typing')
  expect(client.getQueryData<DraftDetail>(draftKeys.detail(draft.id))).toMatchObject({
    body: 'Newer typing',
    body_version: 3,
  })
})

it('restarts an uncached active detail load after cancelling its pre-save response', async () => {
  client.removeQueries({ queryKey: draftKeys.detail(draft.id) })
  vi.mocked(api.getDraft).mockImplementationOnce(() => new Promise(() => {}))
  const { result } = renderHook(
    () => ({ detail: useDraft(draft.id), save: useUpdateBody(draft.id) }),
    { wrapper: Wrapper },
  )
  await advance()
  expect(result.current.detail.isPending).toBe(true)
  await act(async () => {
    await result.current.save.mutateAsync({
      content: 'Saved before first read',
      expected_version: 1,
    })
  })
  await advance()
  expect(result.current.detail.data).toMatchObject({
    body: 'Saved before first read',
    body_version: 2,
  })
  expect(result.current.detail.isPending).toBe(false)
})
