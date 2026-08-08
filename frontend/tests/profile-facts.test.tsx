import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ProfileFacts } from '@/components/profile/profile-facts'
import { api } from '@/lib/api'
import type { ClassProfile } from '@/types'

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
  useParams: () => ({ id: '1' }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => '/classes/1',
}))

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return { wrapper }
}

function profile(overrides: Partial<ClassProfile> = {}): ClassProfile {
  return { facts: [], extraction_skipped_reason: null, ...overrides }
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('ProfileFacts', () => {
  it('offers a way into Settings when the endpoint refused the analysis', async () => {
    // The one extraction failure a student can act on, so it is the one that gets an
    // address: a local server holding a model other than the one the settings name.
    vi.spyOn(api, 'getClassProfile').mockResolvedValue(
      profile({ extraction_skipped_reason: 'endpoint_failed' }),
    )
    const { wrapper } = createWrapper()

    render(<ProfileFacts classId={1} />, { wrapper })

    expect(
      await screen.findByText('The tutor endpoint could not analyze this upload'),
    ).toBeInTheDocument()
    expect(screen.getByText(/different model loaded/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /model and context window/ })).toHaveAttribute(
      'href',
      '/settings#model',
    )
  })

  it('explains a failed analysis without sending anyone to Settings over it', async () => {
    // Nothing here points at a setting, so a link would only be somewhere to be
    // disappointed. The document is uploaded and searchable either way, which is the part
    // worth saying.
    vi.spyOn(api, 'getClassProfile').mockResolvedValue(
      profile({ extraction_skipped_reason: 'extraction_failed' }),
    )
    const { wrapper } = createWrapper()

    render(<ProfileFacts classId={1} />, { wrapper })

    expect(await screen.findByText('Profile extraction did not finish')).toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('says a profile is simply empty when nothing went wrong', async () => {
    vi.spyOn(api, 'getClassProfile').mockResolvedValue(profile())
    const { wrapper } = createWrapper()

    render(<ProfileFacts classId={1} />, { wrapper })

    expect(await screen.findByText('No profile yet')).toBeInTheDocument()
  })
})
