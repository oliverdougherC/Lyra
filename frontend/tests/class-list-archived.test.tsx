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

it('keeps cached classes reachable when refresh fails and retries visibly', async () => {
  const klass = {
    id: 9,
    name: 'Saved Chemistry',
    archived: false,
    last_active_at: '2026-08-01T00:00:00Z',
  } as ClassRead
  vi.spyOn(api, 'listClasses').mockRejectedValue(new Error('offline'))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['classes'], [klass])
  render(
    <QueryClientProvider client={client}>
      <ClassList />
    </QueryClientProvider>,
  )
  await screen.findByText('Could not refresh your classes')
  expect(screen.getByRole('link', { name: /Saved Chemistry/ })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Retry' })).toBeEnabled()
})

it('places the single create action before a populated inventory', async () => {
  vi.spyOn(api, 'listClasses').mockResolvedValue([
    {
      id: 9,
      name: 'Saved Chemistry',
      archived: false,
      last_active_at: '2026-08-01T00:00:00Z',
    } as ClassRead,
  ])
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <ClassList />
    </QueryClientProvider>,
  )
  const course = await screen.findByRole('link', { name: /Saved Chemistry/ })
  const create = screen.getByRole('button', { name: 'New class' })
  expect(create.compareDocumentPosition(course) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
})
