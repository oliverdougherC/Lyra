import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ClassChatsPanel } from '@/components/classes/class-chats-panel'
import { api } from '@/lib/api'
import { chatKeys } from '@/lib/hooks/use-chat'
import type { SessionRead } from '@/types'

afterEach(() => vi.restoreAllMocks())
const saved: SessionRead = {
  id: 7,
  class_id: 1,
  title: 'Saved discussion',
  mode: 'guide',
  artifact_part_id: null,
  created_at: '2026-09-04T12:00:00Z',
}
function mount(cached = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (cached) client.setQueryData(chatKeys.sessions(1), [saved])
  render(
    <QueryClientProvider client={client}>
      <ClassChatsPanel classId={1} />
    </QueryClientProvider>,
  )
  return client
}

describe('Saved conversation list recovery (PLA-405)', () => {
  it('shows failure instead of an empty list, then recovers', async () => {
    vi.spyOn(api, 'listSessions')
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue([saved])
    mount()
    expect(await screen.findByText('Could not load conversations.')).toBeInTheDocument()
    expect(screen.queryByText('No conversations yet')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Retry conversations' }))
    expect(await screen.findByText('Saved discussion')).toBeInTheDocument()
  })

  it('keeps cached discussions visible when refreshing fails', async () => {
    vi.spyOn(api, 'listSessions').mockRejectedValue(new Error('offline'))
    const client = mount(true)
    await act(() => client.invalidateQueries({ queryKey: chatKeys.sessions(1) }))
    await waitFor(() =>
      expect(screen.getByText('Could not load conversations.')).toBeInTheDocument(),
    )
    expect(screen.getByText('Saved discussion')).toBeInTheDocument()
  })
})
