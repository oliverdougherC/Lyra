import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, expect, it, vi } from 'vitest'

import { SettingsForm } from '@/components/settings/settings-form'
import { api } from '@/lib/api'
import { RouterProvider } from '@/router/hooks'
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
    return (
      <QueryClientProvider client={client}>
        <RouterProvider>{children}</RouterProvider>
      </QueryClientProvider>
    )
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/#/settings')
  vi.spyOn(api, 'listClasses').mockResolvedValue([])
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.setPointerCapture = vi.fn()
  Element.prototype.releasePointerCapture = vi.fn()
})

it.each(['extraction-enabled', 'endpoint-url', 'remote-ack', 'model'])(
  'focuses and reveals the %s recovery control after settings load',
  async (anchor) => {
    window.history.replaceState(null, '', `/#/settings#${anchor}`)
    const scroll = vi.spyOn(Element.prototype, 'scrollIntoView')
    vi.spyOn(api, 'getSettings').mockResolvedValue({ ...settings, endpoint_is_local: false })
    render(<SettingsForm />, { wrapper: createWrapper() })

    const target = anchor === 'model' ? 'endpoint-url' : anchor
    await waitFor(() => expect(document.getElementById(target)).toHaveFocus())
    expect(scroll).toHaveBeenCalledWith({ block: 'center', inline: 'nearest' })
    expect(screen.queryByRole('spinbutton', { name: 'Context window' })).not.toBeInTheDocument()
  },
)

it('saves the explicit web-research and parallel capability switches', async () => {
  vi.spyOn(api, 'getSettings').mockResolvedValue(settings)
  const update = vi
    .spyOn(api, 'updateSettings')
    .mockImplementation(async (patch) => ({ ...settings, ...patch }))
  render(<SettingsForm />, { wrapper: createWrapper() })

  await userEvent.click(await screen.findByRole('button', { name: /Web research/ }))
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
  await userEvent.click(await screen.findByRole('combobox', { name: 'History 201 web research' }))
  await userEvent.click(screen.getByRole('option', { name: 'Allow' }))

  expect(update).toHaveBeenCalledWith(7, { allow_web_research: true })
  expect(await screen.findByText('Effective: allowed')).toBeInTheDocument()
})

it('invalidates a tested endpoint and its models as soon as the address changes', async () => {
  vi.spyOn(api, 'getSettings').mockResolvedValue(settings)
  vi.spyOn(api, 'updateSettings').mockImplementation(async (patch) => ({ ...settings, ...patch }))
  vi.spyOn(api, 'testConnection').mockResolvedValue({
    ok: true,
    model_count: 1,
    message: 'Endpoint A connected',
  })
  vi.spyOn(api, 'listModels').mockResolvedValue({ models: ['endpoint-a-model'] })
  render(<SettingsForm />, { wrapper: createWrapper() })
  await userEvent.click(await screen.findByRole('button', { name: 'Test connection' }))
  expect(await screen.findByText('Endpoint A connected')).toBeInTheDocument()
  await userEvent.type(screen.getByRole('textbox', { name: 'Endpoint URL' }), '/new')
  expect(screen.queryByText('Endpoint A connected')).not.toBeInTheDocument()
  expect(screen.getByRole('combobox', { name: 'Model' })).toBeDisabled()
})

it('retains a persistent endpoint save error with a retry action', async () => {
  vi.spyOn(api, 'getSettings').mockResolvedValue(settings)
  vi.spyOn(api, 'updateSettings').mockRejectedValue(new Error('offline'))
  render(<SettingsForm />, { wrapper: createWrapper() })
  const endpoint = await screen.findByRole('textbox', { name: 'Endpoint URL' })
  await userEvent.type(endpoint, '/new')
  await userEvent.tab()
  expect(await screen.findByRole('button', { name: 'Retry Endpoint URL save' })).toBeInTheDocument()
  expect(endpoint).toHaveValue(settings.endpoint_url + '/new')
  expect(screen.getByText(/Endpoint URL: Not saved/)).toBeInTheDocument()
})

it('discards a delayed connection result after credentials change', async () => {
  vi.spyOn(api, 'getSettings').mockResolvedValue(settings)
  vi.spyOn(api, 'updateSettings').mockImplementation(async (patch) => ({ ...settings, ...patch }))
  let finish!: (value: { ok: boolean; model_count: number; message: string }) => void
  const probe = vi.spyOn(api, 'testConnection').mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve
      }),
  )
  vi.spyOn(api, 'listModels').mockResolvedValue({ models: ['old-model'] })
  render(<SettingsForm />, { wrapper: createWrapper() })
  await userEvent.click(await screen.findByRole('button', { name: 'Test connection' }))
  await userEvent.type(screen.getByLabelText('API key'), 'replacement')
  expect(probe).toHaveBeenCalled()
  finish({ ok: true, model_count: 1, message: 'Old key worked' })
  await screen.findByText('Save any new API key, then test the connection to choose a model.')
  expect(screen.queryByText('Old key worked')).not.toBeInTheDocument()
  expect(screen.getByRole('combobox', { name: 'Model' })).toBeDisabled()
})

it('retries the current API key and clears it after successful recovery', async () => {
  vi.spyOn(api, 'getSettings').mockResolvedValue(settings)
  const update = vi
    .spyOn(api, 'updateSettings')
    .mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValue({ ...settings, api_key_set: true })
  render(<SettingsForm />, { wrapper: createWrapper() })
  const input = await screen.findByLabelText('API key')
  await userEvent.type(input, 'first-key')
  await userEvent.click(screen.getAllByRole('button', { name: 'Save key' })[0])
  await screen.findByRole('button', { name: 'Retry API key save' })
  await userEvent.clear(input)
  await userEvent.type(input, 'corrected-key')
  await userEvent.click(screen.getByRole('button', { name: 'Retry API key save' }))
  expect(update).toHaveBeenLastCalledWith({ api_key: 'corrected-key' })
  expect(input).toHaveValue('')
  expect(screen.getByRole('button', { name: 'Test connection' })).toBeEnabled()
})

it('preserves a replacement key typed while saving the previous one', async () => {
  vi.spyOn(api, 'getSettings').mockResolvedValue(settings)
  let finish!: (value: SettingsRead) => void
  vi.spyOn(api, 'updateSettings').mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve
      }),
  )
  render(<SettingsForm />, { wrapper: createWrapper() })
  const input = await screen.findByLabelText('API key')
  await userEvent.type(input, 'first-key')
  await userEvent.click(screen.getAllByRole('button', { name: 'Save key' })[0])
  await userEvent.clear(input)
  await userEvent.type(input, 'newer-key')
  await act(async () => finish({ ...settings, api_key_set: true }))
  expect(input).toHaveValue('newer-key')
  expect(screen.getByRole('button', { name: 'Test connection' })).toBeDisabled()
})

it('serializes repeated endpoint saves and keeps the newest address active', async () => {
  vi.spyOn(api, 'getSettings').mockResolvedValue(settings)
  let finish!: (value: SettingsRead) => void
  const update = vi
    .spyOn(api, 'updateSettings')
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve
        }),
    )
    .mockImplementation(async (patch) => ({ ...settings, ...patch }))
  render(<SettingsForm />, { wrapper: createWrapper() })
  const endpoint = await screen.findByRole('textbox', { name: 'Endpoint URL' })
  await userEvent.clear(endpoint)
  await userEvent.type(endpoint, 'http://localhost:9000/v1')
  await userEvent.tab()
  await userEvent.clear(endpoint)
  await userEvent.type(endpoint, 'http://localhost:9001/v1')
  await userEvent.tab()
  expect(update).toHaveBeenCalledTimes(1)
  await act(async () => finish({ ...settings, endpoint_url: 'http://localhost:9000/v1' }))
  await waitFor(() => expect(update).toHaveBeenCalledTimes(2))
  expect(update).toHaveBeenLastCalledWith({ endpoint_url: 'http://localhost:9001/v1' })
  expect(endpoint).toHaveValue('http://localhost:9001/v1')
  expect(await screen.findByText('Endpoint URL: Saved')).toBeInTheDocument()
})

it('shows acknowledged remote use neutrally and leaves consent available', async () => {
  vi.spyOn(api, 'getSettings').mockResolvedValue({
    ...settings,
    endpoint_is_local: false,
    remote_ack: true,
  })
  render(<SettingsForm />, { wrapper: createWrapper() })
  const consent = await screen.findByRole('switch', { name: /I understand my document/ })
  expect(consent).toBeChecked()
  expect(consent.closest('[data-slot="alert"]')).not.toHaveAttribute('role', 'alert')
  expect(consent.closest('[data-slot="alert"]')).not.toHaveClass('border-danger-text')
})

it.each(['exa-api-key', 'allow-web-research'])(
  'opens optional research and focuses %s from a deep link',
  async (anchor) => {
    window.history.replaceState(null, '', `/#/settings#${anchor}`)
    vi.spyOn(api, 'getSettings').mockResolvedValue(settings)
    render(<SettingsForm />, { wrapper: createWrapper() })
    await waitFor(() => expect(document.getElementById(anchor)).toHaveFocus())
    expect(screen.getByRole('button', { name: /Web research/ })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
  },
)
it('keeps failed research saves visible when the disclosure is activated again', async () => {
  vi.spyOn(api, 'getSettings').mockResolvedValue(settings)
  vi.spyOn(api, 'updateSettings').mockRejectedValue(new Error('offline'))
  render(<SettingsForm />, { wrapper: createWrapper() })
  const disclosure = await screen.findByRole('button', { name: /Web research/ })
  await userEvent.click(disclosure)
  await userEvent.click(screen.getByRole('switch', { name: 'Allow web research' }))
  await screen.findByRole('button', { name: 'Retry Web research save' })
  await userEvent.click(disclosure)
  expect(screen.getByRole('button', { name: 'Retry Web research save' })).toBeVisible()
})

it('preserves loaded settings and edited values when background refresh fails', async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const get = vi.spyOn(api, 'getSettings').mockResolvedValue(settings)
  render(
    <QueryClientProvider client={client}>
      <RouterProvider>
        <SettingsForm />
      </RouterProvider>
    </QueryClientProvider>,
  )
  const endpoint = await screen.findByRole('textbox', { name: 'Endpoint URL' })
  await userEvent.type(endpoint, '/unsaved')
  get.mockRejectedValue(new Error('offline'))
  await act(async () => {
    await client.invalidateQueries({ queryKey: ['settings'] })
  })
  expect(await screen.findByText('Settings could not refresh')).toBeVisible()
  expect(endpoint).toHaveValue(settings.endpoint_url + '/unsaved')
  expect(screen.getByRole('button', { name: 'Retry settings' })).toBeEnabled()
})

it('shows an acknowledged remote badge neutrally without recommending a model location', async () => {
  vi.spyOn(api, 'getSettings').mockResolvedValue({
    ...settings,
    endpoint_url: 'https://tutor.example.invalid/v1',
    endpoint_host: 'tutor.example.invalid',
    endpoint_is_local: false,
    remote_ack: true,
  })
  render(<SettingsForm />, { wrapper: createWrapper() })
  const badge = await screen.findByRole('link', { name: /Remote/ })
  expect(badge).not.toHaveClass('text-danger-text')
  expect(badge).not.toHaveClass('border-danger-text/40')
  expect(screen.queryByText(/works best with a local model server/)).not.toBeInTheDocument()
  expect(
    screen.getByText(/Questions and relevant course passages go to this endpoint/),
  ).toBeVisible()
})

it('does not crash Settings or infer permission from an incomplete class settings response', async () => {
  vi.spyOn(api, 'getSettings').mockResolvedValue(settings)
  vi.mocked(api.listClasses).mockResolvedValue([{ id: 7, name: 'Synthetic class' }] as never)
  vi.spyOn(api, 'getClassWriterSettings').mockResolvedValue({} as never)
  render(<SettingsForm />, { wrapper: createWrapper() })
  await userEvent.click(await screen.findByRole('button', { name: 'Advanced' }))
  expect(await screen.findByText('Could not load research permission.')).toBeVisible()
  expect(screen.getByRole('heading', { name: 'Settings' })).toBeVisible()
  expect(screen.getByRole('combobox', { name: 'Synthetic class web research' })).toBeDisabled()
  expect(screen.getByRole('combobox', { name: 'Synthetic class web research' })).toHaveTextContent(
    'Unavailable',
  )
  expect(screen.queryByText('Effective: blocked')).not.toBeInTheDocument()
})
