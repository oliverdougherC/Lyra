import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, expect, it, vi } from 'vitest'

import { SettingsForm } from '@/components/settings/settings-form'
import { api } from '@/lib/api'
import type { SettingsRead } from '@/types'

const settings: SettingsRead = {
  endpoint_url: 'http://127.0.0.1:8080/v1',
  model: 'local-model',
  context_window: 8192,
  extraction_enabled: true,
  remote_ack: false,
  api_key_set: false,
  api_key_storage: 'file',
  endpoint_is_local: true,
  endpoint_host: '127.0.0.1',
  embedding_model: null,
  embedding_dim: null,
  tools_supported: null,
  tools_message: null,
  vision_supported: null,
  vision_message: null,
  allow_web_research: false,
  parallel_requests: false,
  parallel_concurrency: 1,
  exa_api_key_set: false,
  exa_api_key_storage: 'file',
}

function createWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return function TestWrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'listClasses').mockResolvedValue([])
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.setPointerCapture = vi.fn()
  Element.prototype.releasePointerCapture = vi.fn()
})

it('saves the explicit web-research and parallel capability switches', async () => {
  vi.spyOn(api, 'getSettings').mockResolvedValue(settings)
  const update = vi
    .spyOn(api, 'updateSettings')
    .mockImplementation(async (patch) => ({ ...settings, ...patch }))
  render(<SettingsForm />, { wrapper: createWrapper() })

  await userEvent.click(await screen.findByRole('switch', { name: 'Allow web research' }))
  // Parallel writer tuning moved behind the Advanced disclosure, so the test opens it first.
  await userEvent.click(await screen.findByRole('button', { name: /Advanced/ }))
  await userEvent.click(await screen.findByRole('switch', { name: 'Parallel writer requests' }))

  expect(update).toHaveBeenCalledWith({ allow_web_research: true })
  expect(update).toHaveBeenCalledWith({ parallel_requests: true })
})

it('surfaces an inheritance-aware web-research override for each class', async () => {
  vi.mocked(api.listClasses).mockResolvedValue([
    {
      id: 7,
      name: 'History 201',
      code: null,
      semester: null,
      archived: false,
      document_count: 0,
      created_at: '2026-08-07T00:00:00Z',
      last_active_at: '2026-08-07T00:00:00Z',
    },
  ])
  vi.spyOn(api, 'getSettings').mockResolvedValue(settings)
  vi.spyOn(api, 'getClassWriterSettings').mockResolvedValue({
    overrides: {
      allow_web_research: null,
      parallel_requests: null,
      parallel_concurrency: null,
    },
    effective: {
      allow_web_research: false,
      parallel_requests: false,
      parallel_concurrency: 1,
    },
  })
  const update = vi.spyOn(api, 'updateClassWriterSettings').mockResolvedValue({
    overrides: {
      allow_web_research: true,
      parallel_requests: null,
      parallel_concurrency: null,
    },
    effective: {
      allow_web_research: true,
      parallel_requests: false,
      parallel_concurrency: 1,
    },
  })
  render(<SettingsForm />, { wrapper: createWrapper() })

  await userEvent.click(await screen.findByRole('button', { name: /Advanced/ }))
  await userEvent.click(
    await screen.findByRole('combobox', { name: 'History 201 web research' }),
  )
  await userEvent.click(screen.getByRole('option', { name: 'Allow' }))

  expect(update).toHaveBeenCalledWith(7, { allow_web_research: true })
  expect(await screen.findByText('Effective: allowed')).toBeInTheDocument()
})
