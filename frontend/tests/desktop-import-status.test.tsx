import { RouterProvider } from '@/router/hooks'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { DesktopImportSection } from '@/components/settings/desktop-import-section'
import { api } from '@/lib/api'

beforeEach(() => vi.restoreAllMocks())
function renderImport() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <RouterProvider>
        <DesktopImportSection />
      </RouterProvider>
    </QueryClientProvider>,
  )
}
it('distinguishes service errors from platform availability and retries', async () => {
  vi.spyOn(api, 'getDesktopImportStatus')
    .mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValue({ available: false } as never)
  renderImport()
  expect(await screen.findByText('Could not load import status')).toBeInTheDocument()
  expect(screen.queryByText('Available in the packaged desktop app')).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Retry import status' }))
  expect(await screen.findByText('Available in the packaged desktop app')).toBeInTheDocument()
})
