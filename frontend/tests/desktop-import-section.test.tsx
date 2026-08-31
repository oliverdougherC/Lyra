import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DesktopImportSection } from '@/components/settings/desktop-import-section'
import {
  useCancelDesktopImport,
  useDesktopImportStatus,
  usePreviewDesktopImport,
  useStartDesktopImport,
} from '@/lib/hooks/use-settings'
import { pickDesktopImportDirectory } from '@/lib/runtime'

vi.mock('@/lib/hooks/use-settings', () => ({
  useDesktopImportStatus: vi.fn(),
  usePreviewDesktopImport: vi.fn(),
  useStartDesktopImport: vi.fn(),
  useCancelDesktopImport: vi.fn(),
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
    vi.mocked(pickDesktopImportDirectory).mockResolvedValue({
      selectionToken: 'e'.repeat(64),
      label: 'Old Lyra',
    })
  })

  it('previews a native opaque selection and starts staging without exposing a path', async () => {
    const user = userEvent.setup()
    render(<DesktopImportSection />)

    expect(screen.getByText(/Lyra schema 40/)).toBeInTheDocument()
    expect(screen.getByText('12 entries · 1.0 KiB estimated')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('/private/')

    await user.click(screen.getByRole('button', { name: 'Choose old Lyra folder' }))
    expect(pickDesktopImportDirectory).toHaveBeenCalledTimes(1)
    expect(vi.mocked(usePreviewDesktopImport)().mutateAsync).toHaveBeenCalledWith('e'.repeat(64))

    await user.click(screen.getByRole('button', { name: 'Stage import' }))
    expect(vi.mocked(useStartDesktopImport)().mutateAsync).toHaveBeenCalledWith(
      expect.objectContaining({ selectionToken: 'e'.repeat(64) }),
    )
  })
})
