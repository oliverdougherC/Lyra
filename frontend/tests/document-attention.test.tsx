import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { act } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DocumentsPane } from '@/components/documents/documents-pane'
import { api } from '@/lib/api'
import { RouterProvider } from '@/router/hooks'
import type { DocumentRead, DocumentState, SettingsRead } from '@/types'

/** One row of the fixture list. Index is the list position (newest first). */
function makeDocument(
  index: number,
  id: number,
  filename: string,
  state: DocumentState,
): DocumentRead {
  return {
    id,
    class_id: 1,
    filename,
    mime: 'application/pdf',
    byte_size: 1024,
    state,
    stage_detail: null,
    pages_total: 10,
    pages_done: 10,
    pages_skipped: 0,
    pages_failed: state === 'failed' ? 2 : 0,
    recognize: false,
    error_message:
      state === 'failed' ? 'Lyra could not finish reading this document. Retry it.' : null,
    created_at: new Date(Date.UTC(2026, 7, 1, 12, 0, 0) - index * 60_000).toISOString(),
  }
}

/**
 * A twelve-document class, long enough that the Files tab shows its filter. Two need
 * attention, in list order: lecture-1.pdf (failed, id 41) and scan-1.pdf (unsupported, id 7).
 */
const DOCUMENTS: DocumentRead[] = [
  makeDocument(0, 41, 'lecture-1.pdf', 'failed'),
  makeDocument(1, 3, 'homework_2.pdf', 'ready'),
  makeDocument(2, 9, 'notes-1.pdf', 'ready'),
  makeDocument(3, 11, 'notes-2.pdf', 'ready'),
  makeDocument(4, 13, 'notes-3.pdf', 'ready'),
  makeDocument(5, 7, 'scan-1.pdf', 'unsupported'),
  makeDocument(6, 15, 'notes-4.pdf', 'ready'),
  makeDocument(7, 17, 'notes-5.pdf', 'ready'),
  makeDocument(8, 19, 'notes-6.pdf', 'ready'),
  makeDocument(9, 21, 'notes-7.pdf', 'ready'),
  makeDocument(10, 23, 'notes-8.pdf', 'ready'),
  makeDocument(11, 25, 'notes-9.pdf', 'ready'),
]

function createQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
}

function renderPane(client: QueryClient) {
  return render(
    <RouterProvider>
      <QueryClientProvider client={client}>
        <DocumentsPane classId={1} variant="manage" />
      </QueryClientProvider>
    </RouterProvider>,
  )
}

function resetLocation(url: string) {
  window.history.replaceState({}, '', url)
}

beforeEach(() => {
  sessionStorage.clear()
  vi.restoreAllMocks()
  vi.spyOn(api, 'listDocuments').mockResolvedValue(DOCUMENTS)
  vi.spyOn(api, 'getDocumentStatus').mockResolvedValue({ state: 'ready' } as never)
  vi.spyOn(api, 'getSettings').mockResolvedValue({ vision_supported: true } as SettingsRead)
})

describe('document attention arrival', () => {
  it('stands on the exact affected row: focus, scroll, emphasis, announcement, strip', async () => {
    const scrollIntoView = vi.spyOn(Element.prototype, 'scrollIntoView')
    resetLocation('/#/classes/1?tab=files&lyra-anchor=document-41')

    renderPane(createQueryClient())

    const row = (await screen.findByText('lecture-1.pdf')).closest(
      '[id="document-41"]',
    ) as HTMLElement
    expect(row).toBeTruthy()
    // The arrival moved focus to the row and scrolled the list to it.
    expect(document.activeElement).toBe(row)
    expect(scrollIntoView).toHaveBeenCalledWith({ block: 'center' })
    // The row wears the transient emphasis, and the live region says why we are here.
    expect(row.className).toContain('border-accent-primary')
    expect(
      screen.getByText(/Jumped to lecture-1\.pdf\. 2 documents need attention\./),
    ).toBeInTheDocument()
    // Two affected documents: the transient strip names the position.
    expect(
      screen.getByRole('group', { name: /Documents that need attention, 1 of 2/ }),
    ).toBeInTheDocument()
    expect(screen.getByText('2 need attention')).toBeInTheDocument()
  })

  it('steps through every affected document, wraps, and Back walks them back', async () => {
    const user = userEvent.setup()
    resetLocation('/#/classes/1?tab=files&lyra-anchor=document-41')

    renderPane(createQueryClient())
    await screen.findByText('lecture-1.pdf')

    await user.click(screen.getByRole('button', { name: 'Next document that needs attention' }))

    await waitFor(() =>
      expect(window.location.hash).toBe('#/classes/1?tab=files&lyra-anchor=document-7'),
    )
    await waitFor(() => expect(document.activeElement).toBe(document.getElementById('document-7')))
    expect(screen.getByRole('group', { name: /2 of 2/ })).toBeInTheDocument()
    expect(screen.getByText(/Jumped to scan-1\.pdf/)).toBeInTheDocument()

    // The path wraps: next from the last item returns to the first.
    await user.click(screen.getByRole('button', { name: 'Next document that needs attention' }))
    await waitFor(() =>
      expect(window.location.hash).toBe('#/classes/1?tab=files&lyra-anchor=document-41'),
    )
    await waitFor(() => expect(document.activeElement).toBe(document.getElementById('document-41')))
    expect(screen.getByRole('group', { name: /1 of 2/ })).toBeInTheDocument()

    // Back re-arrives at the previously visited item and stands on it again. The URL
    // turns before the router finishes the popstate sync, so wait for the arrival itself.
    await act(async () => window.history.back())
    await waitFor(() =>
      expect(window.location.hash).toBe('#/classes/1?tab=files&lyra-anchor=document-7'),
    )
    await waitFor(() => expect(document.activeElement).toBe(document.getElementById('document-7')))
  })

  it('borrows a filter clearing to reveal the target, and gives the filter back on dismiss', async () => {
    const user = userEvent.setup()
    sessionStorage.setItem('lyra:class:1:files-query', 'homework')
    resetLocation('/#/classes/1?tab=files&lyra-anchor=document-41')

    renderPane(createQueryClient())

    const filter = await screen.findByRole('searchbox', { name: 'Filter documents by name' })
    expect(filter).toHaveValue('homework')
    // The stored filter hides lecture-1.pdf; the arrival clears it so the row can stand.
    await waitFor(() => expect(filter).toHaveValue(''))
    await waitFor(() => expect(document.activeElement).toBe(document.getElementById('document-41')))
    expect(screen.getByRole('group', { name: /1 of 2/ })).toBeInTheDocument()

    // Dismiss ends the visit: the anchor leaves the URL and the filter comes back.
    await user.click(screen.getByRole('button', { name: 'Dismiss documents that need attention' }))
    await waitFor(() => expect(window.location.hash).toBe('#/classes/1?tab=files'))
    await waitFor(() => expect(filter).toHaveValue('homework'))
    expect(sessionStorage.getItem('lyra:class:1:files-query')).toBe('homework')
    expect(
      screen.queryByRole('group', { name: /Documents that need attention/ }),
    ).not.toBeInTheDocument()
  })

  it('keeps a filter the student typed during the visit instead of overwriting it', async () => {
    const user = userEvent.setup()
    sessionStorage.setItem('lyra:class:1:files-query', 'homework')
    resetLocation('/#/classes/1?tab=files&lyra-anchor=document-41')

    renderPane(createQueryClient())
    const filter = await screen.findByRole('searchbox', { name: 'Filter documents by name' })
    await waitFor(() => expect(filter).toHaveValue(''))

    // The student types their own query mid-visit; that is now their intent.
    await user.type(filter, 'notes')
    await user.click(screen.getByRole('button', { name: 'Dismiss documents that need attention' }))
    await waitFor(() => expect(window.location.hash).toBe('#/classes/1?tab=files'))
    expect(filter).toHaveValue('notes')
    expect(sessionStorage.getItem('lyra:class:1:files-query')).toBe('notes')
  })

  it('gives a single affected document its row and an announcement without the strip', async () => {
    vi.mocked(api.listDocuments).mockResolvedValue([
      ...DOCUMENTS.slice(0, 5),
      makeDocument(5, 7, 'scan-1.pdf', 'ready'),
      ...DOCUMENTS.slice(6),
    ])
    resetLocation('/#/classes/1?tab=files&lyra-anchor=document-41')

    renderPane(createQueryClient())

    const row = (await screen.findByText('lecture-1.pdf')).closest(
      '[id="document-41"]',
    ) as HTMLElement
    expect(document.activeElement).toBe(row)
    expect(screen.getByText(/Jumped to lecture-1\.pdf\. It needs attention\./)).toBeInTheDocument()
    expect(
      screen.queryByRole('group', { name: /Documents that need attention/ }),
    ).not.toBeInTheDocument()
  })

  it('lands on the remaining items when the anchored document is gone, and says so', async () => {
    resetLocation('/#/classes/1?tab=files&lyra-anchor=document-99')

    renderPane(createQueryClient())

    // The named row no longer exists: the visit stands on the first item that still
    // needs attention rather than dying quietly, and the URL keeps naming the place.
    await waitFor(() => expect(document.activeElement).toBe(document.getElementById('document-41')))
    expect(screen.getByRole('group', { name: /1 of 2/ })).toBeInTheDocument()
    expect(window.location.hash).toBe('#/classes/1?tab=files&lyra-anchor=document-99')
  })

  it('announces plainly when the anchored document is gone and nothing needs attention', async () => {
    vi.mocked(api.listDocuments).mockResolvedValue(
      DOCUMENTS.map((document) => ({ ...document, state: 'ready' as DocumentState })),
    )
    resetLocation('/#/classes/1?tab=files&lyra-anchor=document-99')

    renderPane(createQueryClient())
    await screen.findByText('homework_2.pdf')

    expect(screen.getByText('That document is no longer in this class.')).toBeInTheDocument()
    // No strip, no emphasis, and the history entry still names the place it meant.
    expect(
      screen.queryByRole('group', { name: /Documents that need attention/ }),
    ).not.toBeInTheDocument()
    expect(window.location.hash).toBe('#/classes/1?tab=files&lyra-anchor=document-99')
  })

  it('stands on the remaining item when the anchored document resolves while standing on it', async () => {
    // lecture-1 (the anchored id 41) re-ingested to ready; scan-1 still needs attention.
    vi.mocked(api.listDocuments).mockResolvedValue([
      makeDocument(0, 41, 'lecture-1.pdf', 'ready'),
      ...DOCUMENTS.slice(1),
    ])
    resetLocation('/#/classes/1?tab=files&lyra-anchor=document-41')

    renderPane(createQueryClient())

    await waitFor(() => expect(document.activeElement).toBe(document.getElementById('document-7')))
    expect(screen.getByText(/Jumped to scan-1\.pdf\. It needs attention\./)).toBeInTheDocument()
    expect(window.location.hash).toBe('#/classes/1?tab=files&lyra-anchor=document-41')
  })

  it('ends the visit quietly when the last affected document resolves', async () => {
    vi.mocked(api.listDocuments).mockResolvedValue(
      DOCUMENTS.map((document) => ({ ...document, state: 'ready' as DocumentState })),
    )
    resetLocation('/#/classes/1?tab=files&lyra-anchor=document-41')

    renderPane(createQueryClient())
    await screen.findByText('lecture-1.pdf')

    // Everything is ready now: no strip, no emphasis, and the spent anchor is a replace,
    // not a push, so the URL returns to the plain Files tab.
    await waitFor(() => expect(window.location.hash).toBe('#/classes/1?tab=files'))
    expect(
      screen.queryByRole('group', { name: /Documents that need attention/ }),
    ).not.toBeInTheDocument()
    expect((document.getElementById('document-41') as HTMLElement).className).not.toContain(
      'ring-accent-primary',
    )
  })

  it('does not re-focus or re-announce when the list re-polls with the same items', async () => {
    const scrollIntoView = vi.spyOn(Element.prototype, 'scrollIntoView')
    resetLocation('/#/classes/1?tab=files&lyra-anchor=document-41')
    const client = createQueryClient()
    renderPane(client)

    await screen.findByText('lecture-1.pdf')
    expect(document.activeElement).toBe(document.getElementById('document-41'))
    const jumpsBefore = scrollIntoView.mock.calls.length

    // A poll re-asks for the list with the same rows. React Query structurally shares
    // deeply-equal data, so the spy's call count, not the data reference, marks the poll.
    const listSpy = vi.mocked(api.listDocuments)
    listSpy.mockResolvedValue(DOCUMENTS.map((document) => ({ ...document })))
    const refetch = client.invalidateQueries({ queryKey: ['documents', 1] })
    await refetch
    await waitFor(() => expect(listSpy.mock.calls.length).toBeGreaterThanOrEqual(2))

    // One poll must not re-steal focus, re-scroll, or re-announce the same arrival.
    await Promise.resolve()
    expect(scrollIntoView.mock.calls.length).toBe(jumpsBefore)
    expect(document.activeElement).toBe(document.getElementById('document-41'))
  })
})
