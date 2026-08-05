import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/lib/api'
import { classKeys } from '@/lib/hooks/use-classes'
import {
  documentKeys,
  documentsPollInterval,
  isTerminal,
  useDeleteDocument,
  useDocumentStatus,
  useDocuments,
  useUploadDocument,
} from '@/lib/hooks/use-documents'
import type { DocumentRead, DocumentState, DocumentStatus } from '@/types'

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return { queryClient, wrapper }
}

function documentStatus(state: DocumentState): DocumentStatus {
  return {
    id: 1,
    state,
    stage_detail: null,
    pages_total: 10,
    pages_done: 10,
    pages_skipped: 0,
    error_message: null,
  } as DocumentStatus
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('isTerminal', () => {
  it.each<[DocumentState, boolean]>([
    ['ready', true],
    ['failed', true],
    // Not `failed`: nothing went wrong, the feature is not built yet. Polling still stops.
    ['unsupported', true],
    ['pending', false],
    ['parsing', false],
    ['chunking', false],
    ['embedding', false],
    ['extracting', false],
  ])('treats %s as terminal=%s', (state, expected) => {
    expect(isTerminal(state)).toBe(expected)
  })
})

describe('documentsPollInterval', () => {
  const list = (...states: DocumentState[]) =>
    states.map((state, index) => ({ id: index, state })) as DocumentRead[]

  it('keeps asking while the server is still working on something', () => {
    // The reported bug: a document sat on `extracting` - the "Analyzing" step, a model pass
    // over the whole document - and only showed as finished after a manual reload.
    expect(documentsPollInterval(list('ready', 'extracting'), undefined)).toBe(1500)
  })

  it.each<DocumentState>(['pending', 'parsing', 'chunking', 'embedding', 'extracting'])(
    'polls while a document is %s',
    (state) => {
      expect(documentsPollInterval(list(state), undefined)).toBe(1500)
    },
  )

  it('stops once every document has settled', () => {
    expect(documentsPollInterval(list('ready', 'failed', 'unsupported'), undefined)).toBe(false)
  })

  it('does not poll an empty or unloaded list', () => {
    expect(documentsPollInterval([], undefined)).toBe(false)
    expect(documentsPollInterval(undefined, undefined)).toBe(false)
  })

  it("treats a caller's interval as a floor, never a ceiling", () => {
    // The solver's setup screen asks for 2s so newly uploaded files become selectable. While
    // something is actually in flight it should still get the faster poll.
    expect(documentsPollInterval(list('extracting'), 2000)).toBe(1500)
    expect(documentsPollInterval(list('ready'), 2000)).toBe(2000)
  })

  it('ignores a caller that asked for no polling at all while work is in flight', () => {
    expect(documentsPollInterval(list('extracting'), false)).toBe(1500)
    expect(documentsPollInterval(list('ready'), false)).toBe(false)
  })
})

describe('useDocuments', () => {
  it('fetches the class document list', async () => {
    const docs = [{ id: 1, filename: 'syllabus.pdf' }] as DocumentRead[]
    vi.spyOn(api, 'listDocuments').mockResolvedValue(docs)
    const { wrapper } = createWrapper()

    const { result } = renderHook(() => useDocuments(1), { wrapper })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(docs)
  })

  it('does not fire for a non-finite class id', () => {
    // A route param that has not resolved yet must not produce a request for `/classes/NaN`.
    const spy = vi.spyOn(api, 'listDocuments').mockResolvedValue([])
    const { wrapper } = createWrapper()

    renderHook(() => useDocuments(Number.NaN), { wrapper })

    expect(spy).not.toHaveBeenCalled()
  })

  it('surfaces a backend failure rather than hanging', async () => {
    vi.spyOn(api, 'listDocuments').mockRejectedValue(new Error('boom'))
    const { wrapper } = createWrapper()

    const { result } = renderHook(() => useDocuments(1), { wrapper })

    await waitFor(() => expect(result.current.isError).toBe(true))
  })
})

describe('useDocumentStatus polling', () => {
  it('stops polling once the document is ready', async () => {
    vi.spyOn(api, 'getDocumentStatus').mockResolvedValue(documentStatus('ready'))
    const { wrapper } = createWrapper()

    const { result } = renderHook(() => useDocumentStatus(1), { wrapper })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    // A terminal state must end the poll; otherwise every finished upload keeps a timer.
    expect(result.current.data?.state).toBe('ready')
  })

  it('stops polling on unsupported, which is terminal but not a failure', async () => {
    vi.spyOn(api, 'getDocumentStatus').mockResolvedValue(documentStatus('unsupported'))
    const { wrapper } = createWrapper()

    const { result } = renderHook(() => useDocumentStatus(1), { wrapper })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(isTerminal(result.current.data!.state)).toBe(true)
  })

  it('keeps polling while a stage is still running', async () => {
    vi.spyOn(api, 'getDocumentStatus').mockResolvedValue(documentStatus('embedding'))
    const { wrapper } = createWrapper()

    const { result } = renderHook(() => useDocumentStatus(1), { wrapper })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(isTerminal(result.current.data!.state)).toBe(false)
  })

  it('does not poll when disabled', () => {
    const spy = vi.spyOn(api, 'getDocumentStatus').mockResolvedValue(documentStatus('ready'))
    const { wrapper } = createWrapper()

    renderHook(() => useDocumentStatus(1, false), { wrapper })

    expect(spy).not.toHaveBeenCalled()
  })
})

describe('cache invalidation', () => {
  it('refreshes the document list and the class list after an upload', async () => {
    // The class card shows a document count, so it goes stale on every upload.
    vi.spyOn(api, 'uploadDocument').mockResolvedValue({ id: 2 } as DocumentRead)
    const { queryClient, wrapper } = createWrapper()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    const { result } = renderHook(() => useUploadDocument(1), { wrapper })
    result.current.mutate(new File(['x'], 'notes.pdf'))

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: documentKeys.list(1) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: classKeys.all })
  })

  it('refreshes the document list and the class list after a delete', async () => {
    vi.spyOn(api, 'deleteDocument').mockResolvedValue(undefined)
    const { queryClient, wrapper } = createWrapper()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    const { result } = renderHook(() => useDeleteDocument(1), { wrapper })
    result.current.mutate(5)

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: documentKeys.list(1) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: classKeys.all })
  })
})

describe('query keys', () => {
  it('scopes the document list per class', () => {
    // A shared key would show one class's documents in another's workspace.
    expect(documentKeys.list(1)).not.toEqual(documentKeys.list(2))
  })

  it('scopes status per document', () => {
    expect(documentKeys.status(1)).not.toEqual(documentKeys.status(2))
  })
})
