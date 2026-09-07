import { RouterProvider } from '@/router/hooks'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DesktopImportSection } from '@/components/settings/desktop-import-section'
import {
  useCancelDesktopImport,
  useDesktopImportStatus,
  usePreviewDesktopImport,
  useResetDesktopImport,
  useStartDesktopImport,
} from '@/lib/hooks/use-settings'
import { pickDesktopImportDirectory, publishDesktopImport } from '@/lib/runtime'

vi.mock('@/lib/hooks/use-settings', () => ({
  useDesktopImportStatus: vi.fn(),
  usePreviewDesktopImport: vi.fn(),
  useStartDesktopImport: vi.fn(),
  useCancelDesktopImport: vi.fn(),
  useResetDesktopImport: vi.fn(),
}))

vi.mock('@/lib/runtime', () => ({
  pickDesktopImportDirectory: vi.fn(),
  publishDesktopImport: vi.fn(),
}))

const preview = {
  source_name: 'Old Lyra',
  source_kind: 'checkout_root',
  schema_version: 40,
  class_count: 2,
  document_count: 7,
  total_entries: 12,
  total_bytes: 1024,
  sample_entries: ['uploads/1/notes.pdf'],
  warnings: ['The original source remains untouched.'],
  asset_summary: {
    selected_models: 1,
    selected_model_bytes: 100,
    selected_caches: 1,
    selected_cache_bytes: 50,
    preserved_models: 0,
    preserved_model_bytes: 0,
    preserved_caches: 0,
    preserved_cache_bytes: 0,
  },
  old_runtime_active: false,
}

describe('DesktopImportSection', () => {
  beforeEach(() => {
    vi.mocked(useDesktopImportStatus).mockReturnValue({
      data: {
        available: true,
        destination_ready: true,
        status: 'idle',
        phase: null,
        message: null,
        source_name: null,
        copied_entries: 0,
        total_entries: 0,
        copied_bytes: 0,
        total_bytes: 0,
        cancel_requested: false,
        can_resume: false,
        requires_restart: false,
        preview,
      },
      isPending: false,
      isError: false,
      refetch: vi.fn(),
    } as never)
    vi.mocked(usePreviewDesktopImport).mockReturnValue({
      data: preview,
      isPending: false,
      mutateAsync: vi.fn().mockResolvedValue(preview),
    } as never)
    vi.mocked(useStartDesktopImport).mockReturnValue({
      isPending: false,
      mutateAsync: vi.fn().mockResolvedValue({ status: 'queued' }),
    } as never)
    vi.mocked(useCancelDesktopImport).mockReturnValue({
      isPending: false,
      mutate: vi.fn(),
    } as never)
    vi.mocked(useResetDesktopImport).mockReturnValue({
      isPending: false,
      mutateAsync: vi.fn().mockResolvedValue({
        available: true,
        destination_ready: true,
        status: 'idle',
        phase: null,
        message: 'Staged import discarded. Choose a folder to start again.',
        source_name: null,
        copied_entries: 0,
        total_entries: 0,
        copied_bytes: 0,
        total_bytes: 0,
        cancel_requested: false,
        can_resume: false,
        requires_restart: false,
        preview: null,
      }),
    } as never)
    vi.mocked(pickDesktopImportDirectory).mockResolvedValue({
      selectionToken: 'e'.repeat(64),
      label: 'Old Lyra',
    })
  })

  it('previews a native opaque selection and starts staging without exposing a path', async () => {
    const user = userEvent.setup()
    render(
      <RouterProvider>
        <DesktopImportSection />
      </RouterProvider>,
    )

    await user.click(screen.getByRole('button', { name: /Import existing Lyra data/ }))
    expect(screen.getByText(/Database schema 40/)).toBeInTheDocument()
    expect(screen.getByText('1.0 KiB estimated')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('/private/')

    await user.click(screen.getByRole('button', { name: 'Choose old Lyra folder' }))
    expect(pickDesktopImportDirectory).toHaveBeenCalledTimes(1)
    expect(vi.mocked(usePreviewDesktopImport)().mutateAsync).toHaveBeenCalledWith('e'.repeat(64))

    await user.click(screen.getByRole('button', { name: 'Prepare import' }))
    expect(vi.mocked(useStartDesktopImport)().mutateAsync).toHaveBeenCalledWith(
      expect.objectContaining({ selectionToken: 'e'.repeat(64) }),
    )
  })

  it('confirms before discarding a staged import', async () => {
    vi.mocked(useDesktopImportStatus).mockReturnValue({
      data: {
        available: true,
        destination_ready: true,
        status: 'staged',
        phase: 'awaiting_publish',
        message: 'Import staged. Quit and relaunch Lyra to publish it safely.',
        source_name: 'Old Lyra',
        copied_entries: 12,
        total_entries: 12,
        copied_bytes: 1024,
        total_bytes: 1024,
        cancel_requested: false,
        can_resume: false,
        requires_restart: true,
        preview,
      },
      isPending: false,
      isError: false,
      refetch: vi.fn(),
    } as never)

    const user = userEvent.setup()
    render(
      <RouterProvider>
        <DesktopImportSection />
      </RouterProvider>,
    )

    await user.click(screen.getByRole('button', { name: 'Discard staged import' }))
    expect(screen.getByRole('alertdialog')).toHaveTextContent('Discard staged import?')
    expect(vi.mocked(useResetDesktopImport)().mutateAsync).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Discard staged import' }))
    expect(vi.mocked(useResetDesktopImport)().mutateAsync).toHaveBeenCalledTimes(1)
  })
})

it('keeps an existing populated installation neutral until import is chosen', async () => {
  vi.mocked(useDesktopImportStatus).mockReturnValue({
    data: { available: true, destination_ready: false, status: 'idle' },
    isPending: false,
    isError: false,
  } as never)
  vi.mocked(usePreviewDesktopImport).mockReturnValue({ data: null, isPending: false } as never)
  render(
    <RouterProvider>
      <DesktopImportSection />
    </RouterProvider>,
  )
  expect(screen.queryByRole('note')).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Import existing Lyra data' }))
  expect(screen.getByRole('note')).toHaveTextContent('Import needs an empty installation')
  expect(screen.getByRole('note')).not.toHaveClass('border-danger-text')
})

it('retains the publication failure when refreshing import status also fails', async () => {
  const status = {
    available: true,
    destination_ready: true,
    status: 'staged',
    phase: 'awaiting_publish',
    preview: null,
  }
  const query = {
    data: status,
    isPending: false,
    isError: false,
    refetch: vi.fn(async () => {
      query.isError = true
    }),
  }
  vi.mocked(useDesktopImportStatus).mockReturnValue(query as never)
  vi.mocked(usePreviewDesktopImport).mockReturnValue({ data: null, isPending: false } as never)
  vi.mocked(publishDesktopImport).mockRejectedValue(new Error('publish failed'))
  render(
    <RouterProvider>
      <DesktopImportSection />
    </RouterProvider>,
  )
  await userEvent.click(screen.getByRole('button', { name: 'Restart and finish import' }))
  expect(await screen.findByText('Could not load import status')).toBeVisible()
  expect(
    screen.getByText('The staged import was not published. Your prior data was preserved.'),
  ).toBeVisible()
  expect(screen.getByRole('button', { name: 'Retry import status' })).toBeEnabled()
})
