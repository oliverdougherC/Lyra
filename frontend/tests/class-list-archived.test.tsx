import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { ClassList } from '@/components/classes/class-list'
import { api } from '@/lib/api'
import type { ClassRead } from '@/types'

it('opens and restores archived classes directly from the class index', async () => {
  const klass = {
    id: 8,
    name: 'Archived Biology',
    archived: true,
    last_active_at: '2026-08-01T00:00:00Z',
  } as ClassRead
  vi.spyOn(api, 'listClasses').mockResolvedValue([klass])
  const update = vi.spyOn(api, 'updateClass').mockResolvedValue({ ...klass, archived: false })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <ClassList />
    </QueryClientProvider>,
  )
  await userEvent.click(await screen.findByRole('button', { name: /View archived classes/ }))
  expect(screen.getByRole('link', { name: klass.name })).toHaveAttribute('href', '/#/classes/8')
  await userEvent.click(screen.getByRole('button', { name: 'Restore Archived Biology' }))
  expect(update).toHaveBeenCalledWith(8, { archived: false })
})
