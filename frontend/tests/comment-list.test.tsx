import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { CommentList } from '@/components/drafts/comment-list'
import { api } from '@/lib/api'
import type { DraftComment } from '@/types'

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return { wrapper }
}

let nextId = 1

function thread(overrides: Partial<DraftComment>): DraftComment {
  return {
    id: nextId++,
    author: 'reviewer',
    severity: 'major',
    quote: 'the quoted passage',
    body: 'The finding.',
    resolved: 0,
    orphaned: 0,
    anchor: { start: 10, end: 28, exact: true },
    replies: [],
    created_at: '2026-08-07 09:00:00',
    ...overrides,
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
  nextId = 1
})

describe('CommentList', () => {
  it('explains itself when there are no comments', async () => {
    vi.spyOn(api, 'listComments').mockResolvedValue([])
    const { wrapper } = createWrapper()

    render(<CommentList draftId={8} />, { wrapper })

    expect(await screen.findByText(/No comments yet/)).toBeInTheDocument()
  })

  it('sorts open threads by severity, then by where the document reads them', async () => {
    vi.spyOn(api, 'listComments').mockResolvedValue([
      thread({ severity: 'note', body: 'A suggestion.' }),
      thread({
        severity: 'critical',
        body: 'Late in the document.',
        anchor: { start: 90, end: 99, exact: true },
      }),
      thread({
        severity: 'critical',
        body: 'Early in the document.',
        anchor: { start: 5, end: 9, exact: true },
      }),
      thread({ severity: 'minor', body: 'A wording thing.' }),
    ])
    const { wrapper } = createWrapper()

    render(<CommentList draftId={8} />, { wrapper })

    const list = await screen.findByRole('list', { name: 'Open comments' })
    const bodies = within(list)
      .getAllByRole('listitem')
      .map((item) => item.textContent)
    expect(bodies[0]).toContain('Early in the document.')
    expect(bodies[1]).toContain('Late in the document.')
    expect(bodies[2]).toContain('A wording thing.')
    expect(bodies[3]).toContain('A suggestion.')
  })

  it('shows the quote as the anchor and marks whole-document findings', async () => {
    vi.spyOn(api, 'listComments').mockResolvedValue([
      thread({ quote: 'the pendulum swings', body: 'Anchored finding.' }),
      thread({ quote: null, anchor: null, body: 'Document-wide finding.' }),
    ])
    const { wrapper } = createWrapper()

    render(<CommentList draftId={8} />, { wrapper })

    expect(await screen.findByText('the pendulum swings')).toBeInTheDocument()
    expect(screen.getByText('On the whole document')).toBeInTheDocument()
  })

  it('groups orphaned threads under their own heading, quotes kept', async () => {
    vi.spyOn(api, 'listComments').mockResolvedValue([
      thread({ body: 'Still anchored.' }),
      thread({
        orphaned: 1,
        anchor: null,
        quote: 'a deleted passage',
        body: 'The finding survives its anchor.',
      }),
    ])
    const { wrapper } = createWrapper()

    render(<CommentList draftId={8} />, { wrapper })

    const section = await screen.findByRole('region', {
      name: 'Comments on passages that are gone',
    })
    expect(within(section).getByText('a deleted passage')).toBeInTheDocument()
    expect(within(section).getByText('The finding survives its anchor.')).toBeInTheDocument()
    const open = screen.getByRole('list', { name: 'Open comments' })
    expect(within(open).queryByText('The finding survives its anchor.')).not.toBeInTheDocument()
  })

  it('replies through the endpoint and refuses to send blanks', async () => {
    vi.spyOn(api, 'listComments').mockResolvedValue([thread({ id: 12, body: 'A finding.' })])
    const replySpy = vi.spyOn(api, 'replyToComment').mockResolvedValue({
      id: 99,
      author: 'student',
      body: 'On it.',
      created_at: '2026-08-07 09:05:00',
    })
    const { wrapper } = createWrapper()
    render(<CommentList draftId={8} />, { wrapper })

    await userEvent.click(await screen.findByRole('button', { name: /Reply/ }))
    const input = screen.getByRole('textbox', { name: 'Reply to this comment' })
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(replySpy).not.toHaveBeenCalled()
    await userEvent.type(input, 'On it.')
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))

    expect(replySpy).toHaveBeenCalledWith(12, 'On it.')
  })

  it('resolves an open thread and reopens a settled one', async () => {
    vi.spyOn(api, 'listComments').mockResolvedValue([
      thread({ id: 5, body: 'Open finding.' }),
      thread({ id: 6, resolved: 1, body: 'Settled finding.' }),
    ])
    const resolveSpy = vi.spyOn(api, 'resolveComment').mockResolvedValue(thread({ id: 5 }))
    const { wrapper } = createWrapper()
    render(<CommentList draftId={8} />, { wrapper })

    await userEvent.click(await screen.findByRole('button', { name: /Resolve/ }))
    expect(resolveSpy).toHaveBeenCalledWith(5, true)
    await userEvent.click(screen.getByRole('button', { name: /Reopen/ }))
    expect(resolveSpy).toHaveBeenCalledWith(6, false)
  })

  it('starts a targeted pass to address a finding', async () => {
    vi.spyOn(api, 'listComments').mockResolvedValue([
      thread({ id: 14, section_ref: 'methods', body: 'Connect this evidence to the claim.' }),
    ])
    const passSpy = vi.spyOn(api, 'startDraftPass').mockResolvedValue({ id: 8 } as never)
    const { wrapper } = createWrapper()
    render(<CommentList draftId={8} />, { wrapper })

    await userEvent.click(await screen.findByRole('button', { name: 'Address' }))

    expect(passSpy).toHaveBeenCalledWith(8, {
      instruction: 'Address this review finding: Connect this evidence to the claim.',
      sections: ['methods'],
      address_comment_id: 14,
      depth: 'standard',
    })
  })

  it('jumps to the anchor from the quote, and says so when there is none', async () => {
    vi.spyOn(api, 'listComments').mockResolvedValue([
      thread({ id: 3, quote: 'the pendulum swings', body: 'Anchored.' }),
    ])
    const onJump = vi.fn().mockReturnValue(true)
    const { wrapper } = createWrapper()
    render(<CommentList draftId={8} onJump={onJump} />, { wrapper })

    await userEvent.click(
      await screen.findByRole('button', { name: 'Show this passage in the document' }),
    )

    expect(onJump).toHaveBeenCalledTimes(1)
    expect(onJump.mock.calls[0][0].id).toBe(3)
  })

  it('jumps from the whole comment card but not from its action buttons', async () => {
    vi.spyOn(api, 'listComments').mockResolvedValue([
      thread({ id: 4, quote: 'the pendulum swings', body: 'Connect this to the result.' }),
    ])
    vi.spyOn(api, 'resolveComment').mockResolvedValue(thread({ id: 4, resolved: 1 }))
    const onJump = vi.fn().mockReturnValue(true)
    const { wrapper } = createWrapper()
    render(<CommentList draftId={8} onJump={onJump} />, { wrapper })

    await userEvent.click(await screen.findByText('Connect this to the result.'))
    expect(onJump).toHaveBeenCalledTimes(1)

    await userEvent.click(screen.getByRole('button', { name: /Resolve/ }))
    expect(onJump).toHaveBeenCalledTimes(1)
  })

  it('dims resolved threads into their own group and renders replies', async () => {
    vi.spyOn(api, 'listComments').mockResolvedValue([
      thread({
        body: 'An open one.',
        replies: [
          {
            id: 99,
            author: 'writer',
            body: 'I proposed a fix.',
            created_at: '2026-08-07 09:05:00',
          },
        ],
      }),
      thread({ resolved: 1, body: 'A settled one.' }),
    ])
    const { wrapper } = createWrapper()

    render(<CommentList draftId={8} />, { wrapper })

    const resolvedSection = await screen.findByRole('region', { name: 'Resolved comments' })
    expect(within(resolvedSection).getByText('A settled one.')).toBeInTheDocument()
    // The writer's replies read as Lyra's.
    expect(screen.getByText(/Lyra:/)).toBeInTheDocument()
    expect(screen.getByText('I proposed a fix.')).toBeInTheDocument()
  })
})
