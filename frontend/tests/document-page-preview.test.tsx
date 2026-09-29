import { render, screen } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'

import { DocumentPagePreview } from '@/components/documents/document-page-preview'
import { loadProtectedAssetSource } from '@/lib/api'
import { useDocuments } from '@/lib/hooks/use-documents'

vi.mock('@/lib/hooks/use-documents', () => ({ useDocuments: vi.fn() }))
vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  return { ...actual, loadProtectedAssetSource: vi.fn() }
})

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(useDocuments).mockReturnValue({
    data: [{ id: 7, filename: 'signals.pdf', mime: 'application/pdf' }],
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof useDocuments>)
  vi.mocked(loadProtectedAssetSource).mockResolvedValue({ url: 'blob:page-five' })
})

it('opens the exact linked source page through the authenticated asset loader', async () => {
  render(<DocumentPagePreview classId={1} documentId={7} page={5} onClose={vi.fn()} />)
  expect(await screen.findByRole('img', { name: 'signals.pdf, page 5' })).toHaveAttribute(
    'src',
    'blob:page-five',
  )
  expect(loadProtectedAssetSource).toHaveBeenCalledWith(
    '/api/documents/7/pages/5',
    expect.any(AbortSignal),
  )
})

it('does not fetch a document that has left the class', async () => {
  vi.mocked(useDocuments).mockReturnValue({
    data: [],
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof useDocuments>)
  render(<DocumentPagePreview classId={1} documentId={7} page={5} onClose={vi.fn()} />)
  expect(screen.getByRole('alert')).toHaveTextContent('no longer in this class')
  expect(loadProtectedAssetSource).not.toHaveBeenCalled()
})
