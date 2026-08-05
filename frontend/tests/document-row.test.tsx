import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DocumentRow } from '@/components/documents/document-row'
import { api } from '@/lib/api'
import { documentKeys } from '@/lib/hooks/use-documents'
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
        onDelete={noop}
        onStatus={noop}
      />,
      { wrapper },
    )

    expect(await screen.findByText('Interrupted, please retry')).toBeInTheDocument()
    expect(screen.getByText('failed')).toBeInTheDocument()
  })
})
