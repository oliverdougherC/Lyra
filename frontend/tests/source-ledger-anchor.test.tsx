import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { SourceLedger } from '@/components/drafts/source-ledger'
import { RouterProvider, useRouter } from '@/router/hooks'
import type { DraftSource } from '@/types'

const useDraftSources = vi.fn()

vi.mock('@/lib/hooks/use-drafts', () => ({
  useDraftSources: (...args: unknown[]) => useDraftSources(...args),
}))

const SOURCES: DraftSource[] = [
  {
    id: 1,
    class_id: 9,
    document_id: 11,
    source_type: 'course',
    title: 'Lecture 1 notes',
    url: null,
    accessed_at: null,
    excerpts: [],
  },
  {
    id: 2,
    class_id: 9,
    document_id: null,
    source_type: 'web',
    title: 'Worked example',
    url: 'https://example.com/worked-example',
    accessed_at: '2026-08-04T00:00:00Z',
    excerpts: [],
  },
] as DraftSource[]

function resetLocation(url: string) {
  window.history.replaceState({}, '', url)
}

function LedgerHarness() {
  const router = useRouter()

  return (
    <div>
      <button type="button" onClick={() => router.pushAnchor('source-2')}>
        Jump once
      </button>
      <button type="button" onClick={() => router.replaceAnchor('source-2')}>
        Jump twice
      </button>
      <SourceLedger classId={9} />
    </div>
  )
}

describe('SourceLedger source anchors', () => {
  beforeEach(() => {
    useDraftSources.mockReturnValue({
      isPending: false,
      isError: false,
      data: SOURCES,
      refetch: vi.fn(),
    })
    resetLocation('/#/classes/9/drafts/4')
  })

  it('focuses and announces the selected source from the route anchor', () => {
    const scrollIntoView = vi.spyOn(Element.prototype, 'scrollIntoView')
    resetLocation('/#/classes/9/drafts/4?lyra-anchor=source-2')

    render(
      <RouterProvider>
        <SourceLedger classId={9} />
      </RouterProvider>,
    )

    const target = screen.getByText('Worked example').closest('li')
    expect(target).toBeTruthy()
    expect(document.activeElement).toBe(target)
    expect(scrollIntoView).toHaveBeenCalled()
    expect(screen.getByText('Jumped to Worked example.')).toBeInTheDocument()
  })

  it('repeats the jump when the same source is activated again', async () => {
    const user = userEvent.setup()
    const scrollIntoView = vi.spyOn(Element.prototype, 'scrollIntoView')

    render(
      <RouterProvider>
        <LedgerHarness />
      </RouterProvider>,
    )

    await user.click(screen.getByRole('button', { name: 'Jump once' }))
    await user.click(screen.getByRole('button', { name: 'Jump twice' }))

    expect(scrollIntoView).toHaveBeenCalledTimes(2)
    expect(screen.getByText('Jumped to Worked example.')).toBeInTheDocument()
  })

  it('announces missing sources without leaving the current route', () => {
    resetLocation('/#/classes/9/drafts/4?lyra-anchor=source-99')

    render(
      <RouterProvider>
        <SourceLedger classId={9} />
      </RouterProvider>,
    )

    expect(screen.getByText('That source is no longer available.')).toBeInTheDocument()
    expect(window.location.hash).toBe('#/classes/9/drafts/4?lyra-anchor=source-99')
  })
})
