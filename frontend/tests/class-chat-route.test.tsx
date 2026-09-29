import { Suspense, useEffect } from 'react'
import { render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'

import { ClassChatRoute, preloadClassChat } from '@/router/class-chat-route'

const mounted = vi.hoisted(() => vi.fn())
vi.mock('@/app/classes/[id]/chat/page', () => ({
  default: function ChatFixture() {
    useEffect(() => {
      mounted()
    }, [])
    return <div>Conversation</div>
  },
}))

it('preserves a direct-link conversation across rerenders and mounts a preloaded handoff synchronously', async () => {
  const route = () => (
    <Suspense fallback="Loading">
      <ClassChatRoute />
    </Suspense>
  )
  const view = render(route())
  expect(await screen.findByText('Conversation')).toBeVisible()
  view.rerender(route())
  expect(mounted).toHaveBeenCalledTimes(1)
  view.unmount()

  await preloadClassChat()
  render(route())
  expect(screen.getByText('Conversation')).toBeVisible()
  expect(screen.queryByText('Loading')).not.toBeInTheDocument()
  expect(mounted).toHaveBeenCalledTimes(2)
})
