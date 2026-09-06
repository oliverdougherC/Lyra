import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ClassProfileSheet } from '@/components/profile/class-profile-sheet'
import { ClassDetailsSheet } from '@/components/profile/class-details-sheet'
import { profileKeys } from '@/lib/hooks/use-profile'
import { ProfileFacts } from '@/components/profile/profile-facts'
import { api } from '@/lib/api'
import type { ClassProfile, ExtractionSkipReason, FactRead } from '@/types'

vi.mock('@/router/hooks', () => ({
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
  return { wrapper, queryClient }
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
    expect(screen.getByRole('link', { name: /Check endpoint settings/ })).toHaveAttribute(
      'href',
      '/#/settings#endpoint-url',
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

function savedFact(overrides: Partial<FactRead> = {}): FactRead {
  return {
    id: 3,
    class_id: 1,
    kind: 'deadline',
    label: 'Final exam',
    value: 'December 12',
    confidence: 'low',
    confirmed: false,
    rejected: false,
    edited: false,
    source_document_id: 1,
    source_filename: 'syllabus.pdf',
    sources: ['syllabus.pdf'],
    source_writer_id: null,
    source_excerpt_id: null,
    source_title: null,
    source_url: null,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

const skips: [ExtractionSkipReason, string, string | null][] = [
  ['extraction_disabled', 'Automatic profile extraction is turned off', 'extraction-enabled'],
  ['no_endpoint', 'Add a tutor endpoint', 'endpoint-url'],
  ['remote_unacknowledged', 'this endpoint is remote', 'remote-ack'],
  ['unparseable_response', 'could not read the analysis response', null],
  ['endpoint_failed', 'endpoint answered with an error', 'endpoint-url'],
  ['extraction_failed', 'document itself is uploaded and searchable', null],
]

describe('profile recovery', () => {
  it.each(skips)('keeps saved facts and controls with %s', async (reason, body, target) => {
    vi.spyOn(api, 'getClassProfile').mockResolvedValue(
      profile({ facts: [savedFact()], extraction_skipped_reason: reason }),
    )
    render(<ProfileFacts classId={1} />, { wrapper: createWrapper().wrapper })
    expect(await screen.findByRole('button', { name: 'December 12' })).toBeEnabled()
    expect(screen.getByText(new RegExp(body))).toBeInTheDocument()
    expect(screen.getByText('Needs confirmation')).toBeInTheDocument()
    expect(screen.getByText('From syllabus.pdf')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeEnabled()
    expect(screen.queryByText('No profile yet')).not.toBeInTheDocument()
    if (target) expect(screen.getByRole('link')).toHaveAttribute('href', `/#/settings#${target}`)
    else expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('keeps cached facts on refetch failure and clears the notice after retry', async () => {
    const get = vi
      .spyOn(api, 'getClassProfile')
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue(profile({ facts: [savedFact({ value: 'December 13' })] }))
    const { wrapper, queryClient } = createWrapper()
    queryClient.setQueryData(profileKeys.forClass(1), profile({ facts: [savedFact()] }))
    render(<ProfileFacts classId={1} />, { wrapper })
    expect(await screen.findByText('Could not refresh the class profile')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'December 12' })).toBeEnabled()
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('button', { name: 'December 13' })).toBeInTheDocument()
    expect(screen.queryByText('Could not refresh the class profile')).not.toBeInTheDocument()
    expect(get).toHaveBeenCalledTimes(2)
  })

  it('offers full-load recovery only when no saved data exists', async () => {
    vi.spyOn(api, 'getClassProfile')
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue(profile())
    render(<ProfileFacts classId={1} />, { wrapper: createWrapper().wrapper })
    expect(await screen.findByText('Could not load the class profile')).toBeInTheDocument()
    expect(screen.queryByText('No profile yet')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('No profile yet')).toBeInTheDocument()
  })

  it.each(['future_reason', '__proto__', 'constructor', { invalid: true }])(
    'handles unknown skip metadata %j',
    async (reason) => {
      vi.spyOn(api, 'getClassProfile').mockResolvedValue({
        facts: [savedFact()],
        extraction_skipped_reason: reason,
      } as ClassProfile)
      render(<ProfileFacts classId={1} />, { wrapper: createWrapper().wrapper })
      expect(await screen.findByText('Profile extraction did not finish')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'December 12' })).toBeEnabled()
    },
  )

  it.each([null, {}, { facts: null }, { facts: {} }, { facts: [null, { kind: 'topic' }] }])(
    'handles malformed profiles %j',
    async (payload) => {
      vi.spyOn(api, 'getClassProfile').mockResolvedValue(payload as ClassProfile)
      render(<ProfileFacts classId={1} />, { wrapper: createWrapper().wrapper })
      expect(await screen.findByText('Some profile details could not be read')).toBeInTheDocument()
      expect(screen.queryByText('No profile yet')).not.toBeInTheDocument()
    },
  )

  it('retains valid rows and provenance when other rows are malformed', async () => {
    vi.spyOn(api, 'getClassProfile').mockResolvedValue(
      profile({
        facts: [
          null,
          savedFact({ confirmed: true, source_url: 'https://example.com/syllabus' }),
        ] as FactRead[],
      }),
    )
    render(<ProfileFacts classId={1} />, { wrapper: createWrapper().wrapper })
    expect(await screen.findByRole('button', { name: 'December 12' })).toBeInTheDocument()
    expect(screen.getByText('Confirmed')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'From syllabus.pdf' })).toHaveAttribute(
      'href',
      'https://example.com/syllabus',
    )
    expect(screen.getByText('Some profile details could not be read')).toBeInTheDocument()
  })

  it.each([
    [{ confirmed: true }, 'Confirmed'],
    [{ rejected: true, confidence: 'high' }, 'Rejected'],
  ] as const)(
    'preserves saved fact status %j during extraction recovery',
    async (status, label) => {
      vi.spyOn(api, 'getClassProfile').mockResolvedValue(
        profile({
          facts: [savedFact(status)],
          extraction_skipped_reason: 'endpoint_failed',
        }),
      )
      render(<ProfileFacts classId={1} />, { wrapper: createWrapper().wrapper })
      expect(await screen.findByText(label)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'December 12' })).toBeInTheDocument()
      expect(screen.queryByText(/uses them in answers/)).not.toBeInTheDocument()
    },
  )

  it('recovers from a malformed response after retry', async () => {
    vi.spyOn(api, 'getClassProfile')
      .mockResolvedValueOnce({} as ClassProfile)
      .mockResolvedValue(profile({ facts: [savedFact()] }))
    render(<ProfileFacts classId={1} />, { wrapper: createWrapper().wrapper })
    expect(await screen.findByText('Some profile details could not be read')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('button', { name: 'December 12' })).toBeInTheDocument()
    expect(screen.queryByText('Some profile details could not be read')).not.toBeInTheDocument()
  })

  it('removes a skip notice after a successful profile update', async () => {
    vi.spyOn(api, 'getClassProfile').mockResolvedValue(
      profile({ facts: [savedFact()], extraction_skipped_reason: 'no_endpoint' }),
    )
    const { wrapper, queryClient } = createWrapper()
    render(<ProfileFacts classId={1} />, { wrapper })
    expect(await screen.findByText('Profile extraction is paused')).toBeInTheDocument()
    act(() => queryClient.setQueryData(profileKeys.forClass(1), profile({ facts: [savedFact()] })))
    await waitFor(() =>
      expect(screen.queryByText('Profile extraction is paused')).not.toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: 'December 12' })).toBeInTheDocument()
  })

  it('shows correction failure and preserves the saved value, then supports retry', async () => {
    vi.spyOn(api, 'getClassProfile').mockResolvedValue(profile({ facts: [savedFact()] }))
    vi.spyOn(api, 'correctClassFact')
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue(profile({ facts: [savedFact({ value: 'December 14', edited: true })] }))
    render(<ProfileFacts classId={1} />, { wrapper: createWrapper().wrapper })
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'December 12' }))
    await user.clear(screen.getByRole('textbox'))
    await user.type(screen.getByRole('textbox'), 'December 14{Enter}')
    expect(await screen.findByText('Could not save the profile change')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'December 12' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'December 12' }))
    await user.keyboard('{Enter}')
    expect(await screen.findByRole('button', { name: 'December 14' })).toBeInTheDocument()
    expect(screen.queryByText('Could not save the profile change')).not.toBeInTheDocument()
  })

  it.each(['confirm', 'reject'] as const)('keeps facts when %s fails', async (action) => {
    vi.spyOn(api, 'getClassProfile').mockResolvedValue(profile({ facts: [savedFact()] }))
    const resolve = vi.spyOn(api, 'resolveClassFact').mockRejectedValue(new Error('offline'))
    render(<ProfileFacts classId={1} />, { wrapper: createWrapper().wrapper })
    await userEvent.click(
      await screen.findByRole('button', { name: action === 'confirm' ? 'Confirm' : 'Reject' }),
    )
    expect(await screen.findByText('Could not save the profile change')).toBeInTheDocument()
    expect(resolve).toHaveBeenCalledWith(1, 3, action)
    expect(screen.getByRole('button', { name: 'December 12' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm' })).toBeEnabled())
  })

  it.each([ClassProfileSheet, ClassDetailsSheet])(
    'shares recovery behavior in the sheet %j',
    async (Surface) => {
      vi.spyOn(api, 'getClassProfile').mockResolvedValue(
        profile({ facts: [savedFact()], extraction_skipped_reason: 'extraction_disabled' }),
      )
      render(<Surface classId={1} open onOpenChange={() => {}} />, {
        wrapper: createWrapper().wrapper,
      })
      expect(await screen.findByRole('button', { name: 'December 12' })).toBeInTheDocument()
      expect(screen.getByText('Profile extraction is paused')).toBeInTheDocument()
    },
  )
})
