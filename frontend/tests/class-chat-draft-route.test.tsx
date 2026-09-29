import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { api } from '@/lib/api'
import { readChatDraft, resetChatDraftMemory, writeChatDraft } from '@/lib/chat-draft-store'
import { AppRoutes } from '@/router/app-routes'
import { preloadClassChat } from '@/router/class-chat-route'
import { RouterProvider, useRouter } from '@/router/hooks'

vi.mock('@/components/agent/work-surface', () => ({ AgentWorkSurface: () => null }))
vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  return {
    ...actual,
    api: {
      ...actual.api,
      getClass: vi.fn(),
      listSessions: vi.fn(),
      listMessages: vi.fn(),
      listDocuments: vi.fn(),
      getSettings: vi.fn(),
      getClassProfile: vi.fn(),
      getAgentWorkspace: vi.fn(),
      sendAgentChat: vi.fn(),
    },
  }
})

function RouteControls() {
  const router = useRouter()
  return (
    <>
      <button type="button" onClick={() => router.push('/elsewhere')}>
        Leave chat
      </button>
      <button type="button" onClick={() => router.push('/classes/1/chat?session=7')}>
        Return to chat
      </button>
      <AppRoutes />
    </>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  resetChatDraftMemory()
  window.history.replaceState({}, '', '/#/classes/1/chat?session=7')
  vi.mocked(api.getClass).mockResolvedValue({ id: 1, name: 'Signals' } as Awaited<
    ReturnType<typeof api.getClass>
  >)
  vi.mocked(api.listSessions).mockResolvedValue([])
  vi.mocked(api.listMessages).mockResolvedValue([])
  vi.mocked(api.listDocuments).mockResolvedValue([])
  vi.mocked(api.getSettings).mockResolvedValue({
    endpoint_url: 'http://localhost:1234/v1',
  } as Awaited<ReturnType<typeof api.getSettings>>)
  vi.mocked(api.getClassProfile).mockResolvedValue({
    facts: [],
    extraction_skipped_reason: null,
  })
  vi.mocked(api.getAgentWorkspace).mockResolvedValue(null)
})

it('settles a restored prompt through the production class route after navigating away', async () => {
  const scope = '1:agent:0:7'
  writeChatDraft(scope, 'Restored route question')
  resetChatDraftMemory()
  let finish!: (value: Awaited<ReturnType<typeof api.sendAgentChat>>) => void
  vi.mocked(api.sendAgentChat).mockReturnValue(
    new Promise((resolve) => {
      finish = resolve
    }),
  )
  await preloadClassChat()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <RouterProvider>
          <RouteControls />
        </RouterProvider>
      </TooltipProvider>
    </QueryClientProvider>,
  )
  expect(await screen.findByLabelText('Message Lyra')).toHaveValue('Restored route question')
  fireEvent.click(screen.getByLabelText('Send message'))
  await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(1))
  fireEvent.click(screen.getByRole('button', { name: 'Leave chat' }))
  fireEvent.click(screen.getByRole('button', { name: 'Return to chat' }))
  expect(await screen.findByLabelText('Message Lyra')).toHaveValue('Restored route question')
  expect(screen.getByLabelText('Send message')).toBeDisabled()
  await act(async () =>
    finish({
      message_id: 42,
      content: 'Answer',
      stopped: 'complete',
      detail: 'Complete.',
      activity: [],
      source_ids: [],
      workspace_change_ids: [],
      command_request_ids: [],
      profile_fact_ids: [],
    }),
  )
  await waitFor(() => expect(screen.getByLabelText('Message Lyra')).toHaveValue(''))
  expect(readChatDraft(scope)).toBeNull()
  expect(screen.getByLabelText('Send message')).toBeDisabled()
  fireEvent.click(screen.getByLabelText('Send message'))
  expect(api.sendAgentChat).toHaveBeenCalledTimes(1)
})

it('preserves a newer question typed in the returned route while the prior send settles', async () => {
  const scope = '1:agent:0:7'
  let finish!: (value: Awaited<ReturnType<typeof api.sendAgentChat>>) => void
  vi.mocked(api.sendAgentChat).mockReturnValue(
    new Promise((resolve) => {
      finish = resolve
    }),
  )
  await preloadClassChat()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <RouterProvider>
          <RouteControls />
        </RouterProvider>
      </TooltipProvider>
    </QueryClientProvider>,
  )
  const box = await screen.findByLabelText('Message Lyra')
  fireEvent.change(box, { target: { value: 'Question A' } })
  fireEvent.click(screen.getByLabelText('Send message'))
  await waitFor(() => expect(api.sendAgentChat).toHaveBeenCalledTimes(1))
  fireEvent.click(screen.getByRole('button', { name: 'Leave chat' }))
  fireEvent.click(screen.getByRole('button', { name: 'Return to chat' }))
  const returned = await screen.findByLabelText('Message Lyra')
  fireEvent.change(returned, { target: { value: 'Question B' } })
  await act(async () =>
    finish({
      message_id: 43,
      content: 'Answer',
      stopped: 'complete',
      detail: 'Complete.',
      activity: [],
      source_ids: [],
      workspace_change_ids: [],
      command_request_ids: [],
      profile_fact_ids: [],
    }),
  )
  expect(returned).toHaveValue('Question B')
  expect(readChatDraft(scope)?.value).toBe('Question B')
  expect(screen.queryByText(/saved copy could not be removed/i)).not.toBeInTheDocument()
  expect(api.sendAgentChat).toHaveBeenCalledTimes(1)
})
