import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'

import ClassHubPage from '@/app/classes/[id]/page'
import { RouterProvider } from '@/router/hooks'

vi.mock('@/lib/hooks/use-classes', () => ({
  useClass: () => ({ data: { id: 1, name: 'Synthetic class' }, isError: false }),
}))
vi.mock('@/components/classes/class-hub', async () => {
  const actual = await vi.importActual<typeof import('@/components/classes/class-hub')>(
    '@/components/classes/class-hub',
  )
  return { ...actual, ClassHub: () => <div>Files in synthetic class</div> }
})
vi.mock('@/components/documents/document-page-preview', () => ({
  DocumentPagePreview: ({
    documentId,
    page,
    onClose,
  }: {
    documentId: number
    page: number
    onClose: () => void
  }) => (
    <button type="button" onClick={onClose}>
      Preview document {documentId} page {page}
    </button>
  ),
}))

beforeEach(() => {
  window.history.replaceState({}, '', '/#/classes/1?tab=files&source-document=7&source-page=5')
})

it('keeps the exact source page open after the navigation anchor is consumed', async () => {
  const user = userEvent.setup()
  render(
    <RouterProvider>
      <ClassHubPage />
    </RouterProvider>,
  )
  expect(screen.getByRole('button', { name: 'Preview document 7 page 5' })).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Preview document 7 page 5' }))
  expect(window.location.hash).toBe('#/classes/1?tab=files')
})
