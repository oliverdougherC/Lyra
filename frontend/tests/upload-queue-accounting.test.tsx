import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { expect, it, vi } from 'vitest'

import { ApiError, api } from '@/lib/api'
import { useUploadQueue } from '@/lib/hooks/use-documents'

it('keeps every failed item when a folder contains more than thirty files', async () => {
  vi.spyOn(api, 'uploadDocument').mockRejectedValue(new ApiError(413, 'File unavailable'))
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  const { result } = renderHook(() => useUploadQueue(938411), { wrapper })
  act(() => {
    result.current.enqueue(
      Array.from({ length: 31 }, (_, index) => new File(['test'], `file-${index}.txt`)),
    )
  })
  await waitFor(() =>
    expect(result.current.attempts.every((item) => item.state === 'failed')).toBe(true),
  )
  expect(result.current.attempts).toHaveLength(31)
})
