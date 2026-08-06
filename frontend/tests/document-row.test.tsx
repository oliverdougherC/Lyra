import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DocumentRow } from '@/components/documents/document-row'
import { api } from '@/lib/api'
import { documentKeys } from '@/lib/hooks/use-documents'
import type { DocumentRead, DocumentState, DocumentStatus, SettingsRead } from '@/types'

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return { queryClient, wrapper }
}

function documentAt(state: DocumentState): DocumentRead {
  return {
    id: 7,
    class_id: 1,
    filename: 'lecture-2.pdf',
    mime: 'application/pdf',
    byte_size: 2048,
    state,
    stage_detail: null,
    pages_total: 32,
    pages_done: 32,
    pages_skipped: 0,
    pages_failed: 0,
    recognize: false,
    error_message: null,
    created_at: '2026-08-05 09:00:00',
  }
}

function statusAt(state: DocumentState): DocumentStatus {
  return {
    state,
    stage_detail: null,
    pages_total: 32,
    pages_done: 32,
    pages_skipped: 0,
    pages_failed: 0,
    recognize: false,
    error_message: null,
  } as DocumentStatus
}

const noop = () => {}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('DocumentRow', () => {
  it('shows the stage its own poll reports while ingestion is running', async () => {
    vi.spyOn(api, 'getDocumentStatus').mockResolvedValue(statusAt('embedding'))
    const { wrapper } = createWrapper()

    render(
      <DocumentRow
        document={documentAt('parsing')}
        selected={false}
        onSelect={noop}
        onRetry={noop}
        onRecognize={noop}
        onDelete={noop}
        onStatus={noop}
      />,
      { wrapper },
    )

    // The row is finer grained than the list while there is something to be fine about.
    expect(await screen.findByText(/Indexing/)).toBeInTheDocument()
  })

  it('drops its frozen poll the moment the list says the document has finished', async () => {
    // The reported bug: a row sat on "Analyzing" for a document the server had finished,
    // and only a page reload cleared it. The status query is disabled once the list reports
    // a terminal state, and a disabled query keeps its last answer - so the row was
    // rendering a snapshot of a stage that had ended, indefinitely.
    vi.spyOn(api, 'getDocumentStatus').mockResolvedValue(statusAt('extracting'))
    const { queryClient, wrapper } = createWrapper()
    queryClient.setQueryData(documentKeys.status(7), statusAt('extracting'))

    render(
      <DocumentRow
        document={documentAt('ready')}
        selected={false}
        onSelect={noop}
        onRetry={noop}
        onRecognize={noop}
        onDelete={noop}
        onStatus={noop}
      />,
      { wrapper },
    )

    expect(await screen.findByText('Ready')).toBeInTheDocument()
    // Not the stage the frozen poll last saw.
    await waitFor(() => expect(screen.queryByText(/Analyzing/)).not.toBeInTheDocument())
  })

  it('reports a failure the list learned about, not the stage it last polled', async () => {
    vi.spyOn(api, 'getDocumentStatus').mockResolvedValue(statusAt('chunking'))
    const { queryClient, wrapper } = createWrapper()
    queryClient.setQueryData(documentKeys.status(7), statusAt('chunking'))

    render(
      <DocumentRow
        document={{ ...documentAt('failed'), error_message: 'Interrupted, please retry' }}
        selected={false}
        onSelect={noop}
        onRetry={noop}
        onRecognize={noop}
        onDelete={noop}
        onStatus={noop}
      />,
      { wrapper },
    )

    expect(await screen.findByText('Interrupted, please retry')).toBeInTheDocument()
    expect(screen.getByText('failed')).toBeInTheDocument()
  })
})

describe('DocumentRow, text recognition', () => {
  /**
   * Stubbed at the API rather than seeded into the cache.
   *
   * Seeding was not enough and the difference was invisible until a dev server happened to
   * be running: the settings query still fetches on mount, jsdom reached the real backend
   * on its default port, and the answer that came back replaced the one the test had set.
   * A test that passes only while nothing is listening is not testing anything.
   */
  function withVision(supported: boolean | null) {
    vi.spyOn(api, 'getSettings').mockResolvedValue({
      vision_supported: supported,
      vision_message: supported === false ? 'It answered 00000.' : null,
    } as SettingsRead)
    return createWrapper()
  }

  function renderRow(document: DocumentRead, onRecognize: () => void, supported: boolean | null) {
    const { wrapper } = withVision(supported)
    render(
      <DocumentRow
        document={document}
        selected={false}
        onSelect={noop}
        onRetry={noop}
        onRecognize={onRecognize}
        onDelete={noop}
        onStatus={noop}
      />,
      { wrapper },
    )
  }

  it('offers to read a scanned document, and does not promise it will happen by itself', async () => {
    // The copy this replaced said scans would be readable "in a future update" and that the
    // document would "process automatically then". Recognition is opt-in, so a student who
    // believed the second half would have waited forever.
    const onRecognize = vi.fn()
    renderRow(documentAt('unsupported'), onRecognize, true)

    await userEvent.click(screen.getByText(/No readable text/))

    expect(screen.getByText(/Lyra can read it now/)).toBeInTheDocument()
    expect(screen.queryByText(/future update/)).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Read this document' }))
    expect(onRecognize).toHaveBeenCalledWith(7)
  })

  it('withholds the offer when the endpoint cannot see, and says where to fix it', async () => {
    // A feature that is unavailable says so plainly and points at the thing that would make
    // it available. It never renders as a failure of the document.
    const onRecognize = vi.fn()
    renderRow(documentAt('unsupported'), onRecognize, false)

    await userEvent.click(screen.getByText(/No readable text/))

    // Awaited: `blind` is false until the settings query answers, so the offer is on screen
    // for a frame first and a synchronous assertion here would be racing that frame.
    expect(await screen.findByText(/needs a model that can see images/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Read this document' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: /endpoint settings/ })).toHaveAttribute(
      'href',
      '/settings',
    )
  })

  it('still offers to read when nobody has asked the endpoint yet', async () => {
    // Null is not a no. Refusing to offer on an unknown would hide the feature from every
    // student who has never opened Settings.
    renderRow(documentAt('unsupported'), vi.fn(), null)

    await userEvent.click(screen.getByText(/No readable text/))

    expect(screen.getByRole('button', { name: 'Read this document' })).toBeInTheDocument()
  })

  it('reports pages that could not be read without calling the document failed', async () => {
    // Thirty-nine good pages and one bad one is a document that works.
    const onRecognize = vi.fn()
    renderRow({ ...documentAt('ready'), pages_failed: 3 }, onRecognize, true)

    expect(await screen.findByText('3 pages could not be read')).toBeInTheDocument()
    expect(screen.queryByText('failed')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Try those pages' }))
    expect(onRecognize).toHaveBeenCalledWith(7)
  })

  it('counts pages only while recognition is running', async () => {
    vi.spyOn(api, 'getDocumentStatus').mockResolvedValue({
      ...statusAt('parsing'),
      stage_detail: 'recognizing',
      pages_total: 608,
      pages_done: 40,
    })
    renderRow(documentAt('parsing'), vi.fn(), true)

    // The page it is on, not the page count. Every other stage writes `pages_done` once at
    // the very end, so a counter there would sit at "page 1 of 608" from first frame to last.
    expect(await screen.findByText(/Reading page 41 of 608/)).toBeInTheDocument()
  })

  it('shows the size rather than a counter for a stage that has no per-page progress', async () => {
    vi.spyOn(api, 'getDocumentStatus').mockResolvedValue(statusAt('parsing'))
    renderRow(documentAt('parsing'), vi.fn(), true)

    expect(await screen.findByText(/32 pages/)).toBeInTheDocument()
    expect(screen.queryByText(/Reading page/)).not.toBeInTheDocument()
  })

  it('discloses the structure it indexed, and says plainly when there is none', async () => {
    // Pillar 3. A student whose book was read as one flat blob has no other way to find out
    // except by noticing the answers got worse.
    vi.spyOn(api, 'getDocumentOutline').mockResolvedValue({
      sections: [],
      chunk_count: 596,
      sectioned_count: 0,
    })
    renderRow(documentAt('ready'), vi.fn(), true)

    await userEvent.click(screen.getByText('Structure Lyra found'))

    expect(await screen.findByText(/No sections found/)).toBeInTheDocument()
    expect(screen.getByText(/596 passages/)).toBeInTheDocument()
  })

  it('lists the sections it found, deepest levels included', async () => {
    vi.spyOn(api, 'getDocumentOutline').mockResolvedValue({
      sections: [
        {
          path: 'Vector Spaces',
          number: '4',
          depth: 1,
          first_page: 90,
          last_page: 91,
          chunk_count: 2,
        },
        {
          path: 'Vector Spaces / Subspaces',
          number: '4.1',
          depth: 2,
          first_page: 92,
          last_page: 95,
          chunk_count: 5,
        },
      ],
      chunk_count: 7,
      sectioned_count: 7,
    })
    renderRow(documentAt('ready'), vi.fn(), true)

    await userEvent.click(screen.getByText('Structure Lyra found'))

    // The leaf title, not the whole path: the levels above it are already on screen above it.
    expect(await screen.findByText('Subspaces')).toBeInTheDocument()
    expect(screen.getByText('4.1')).toBeInTheDocument()
    expect(screen.getByText('p92')).toBeInTheDocument()
  })

  it('does not fetch the outline until the disclosure is opened', async () => {
    const outline = vi.spyOn(api, 'getDocumentOutline')
    renderRow(documentAt('ready'), vi.fn(), true)

    expect(await screen.findByText('Structure Lyra found')).toBeInTheDocument()
    // A closed disclosure is the default on every row, and this is a group-by over every
    // chunk of what may be a 600-page book.
    expect(outline).not.toHaveBeenCalled()
  })
})
