import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { toast } from 'sonner'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { LiveDraftSuggestionPanel } from '@/components/drafts/live-draft-suggestion'
import { ApiError, api } from '@/lib/api'
import type { LiveDraftSuggestion, PendingEdit } from '@/types'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
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

const LIVE_SUGGESTION: LiveDraftSuggestion = {
  id: 14,
  artifact_id: 3,
  run_id: 22,
  status: 'ready',
  stage: 'transitions',
  stage_detail: 'Bridging sections into one argument',
  version: 4,
  base_content: 'Base document.',
  blocks: [
    {
      id: 101,
      block_key: '1.1:intro',
      section_ref: '1.1',
      ordinal: 1,
      kind: 'paragraph',
      heading: 'Introduction',
      content: 'Original introduction.',
      status: 'drafted',
      target_words: 180,
      summary: 'Set up the thesis.',
      revision: 2,
      user_revision: 0,
    },
    {
      id: 102,
      block_key: '1.2:claims',
      section_ref: '1.2',
      ordinal: 2,
      kind: 'paragraph',
      heading: 'Claims',
      content: 'Support the argument.',
      status: 'revised',
      target_words: 260,
      summary: null,
      revision: 5,
      user_revision: 1,
    },
  ],
}

const PENDING_EDIT: PendingEdit = {
  id: 88,
  stale: false,
  note: 'Review the merged draft',
  proposed_content: 'Merged document.',
  hunks: [],
}

function renderPanel(suggestion = LIVE_SUGGESTION, onFinalized = vi.fn(), onOpenPlan = vi.fn()) {
  const { wrapper } = createWrapper()
  const view = render(
    <LiveDraftSuggestionPanel
      draftId={3}
      suggestion={suggestion}
      onFinalized={onFinalized}
      onOpenPlan={onOpenPlan}
    />,
    {
      wrapper,
    },
  )
  return { onFinalized, onOpenPlan, ...view }
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('LiveDraftSuggestionPanel', () => {
  it('renders fixed drafting stages and per-block targets in a separate live suggestion panel', () => {
    renderPanel()

    expect(screen.getByText('Live draft')).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Drafting stages' })).toHaveTextContent(
      'Gathering: doneOutline: doneDrafting: doneTransitions: activeReview: upcomingFinalize: upcomingComplete: upcoming',
    )
    expect(screen.getByText('Bridging sections into one argument')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Introduction draft block' })).toHaveValue(
      'Original introduction.',
    )
    expect(screen.getByText('Target 180 words')).toBeInTheDocument()
    expect(screen.getByText('Target 260 words')).toBeInTheDocument()
    expect(screen.getByText('Stage 4 of 7')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Draft body' })).toBeInTheDocument()
  })

  it('keeps planning canonical in the plan view', async () => {
    const { onOpenPlan } = renderPanel()

    await userEvent.click(screen.getByRole('button', { name: 'View plan' }))

    expect(onOpenPlan).toHaveBeenCalledTimes(1)
  })

  it('follows polled server updates for blocks that are not locally dirty', () => {
    const { rerender } = renderPanel()

    rerender(
      <LiveDraftSuggestionPanel
        draftId={3}
        suggestion={{
          ...LIVE_SUGGESTION,
          blocks: [
            {
              ...LIVE_SUGGESTION.blocks[0],
              content: 'Server refreshed introduction.',
              revision: 3,
            },
            LIVE_SUGGESTION.blocks[1],
          ],
        }}
        onFinalized={vi.fn()}
      />,
    )

    expect(screen.getByRole('textbox', { name: 'Introduction draft block' })).toHaveValue(
      'Server refreshed introduction.',
    )
  })

  it('preserves unsaved local edits when the same block changes on a later poll', async () => {
    const { rerender } = renderPanel()

    await userEvent.clear(screen.getByRole('textbox', { name: 'Introduction draft block' }))
    await userEvent.type(
      screen.getByRole('textbox', { name: 'Introduction draft block' }),
      'Local unsaved introduction.',
    )

    rerender(
      <LiveDraftSuggestionPanel
        draftId={3}
        suggestion={{
          ...LIVE_SUGGESTION,
          blocks: [
            {
              ...LIVE_SUGGESTION.blocks[0],
              content: 'Server refreshed introduction.',
              revision: 3,
            },
            LIVE_SUGGESTION.blocks[1],
          ],
        }}
        onFinalized={vi.fn()}
      />,
    )

    expect(screen.getByRole('textbox', { name: 'Introduction draft block' })).toHaveValue(
      'Local unsaved introduction.',
    )
  })

  it('saves a block with its expected revision and finalizes into the existing suggestion review flow', async () => {
    vi.spyOn(api, 'updateLiveDraftSuggestionBlock').mockResolvedValue({
      ...LIVE_SUGGESTION.blocks[0],
      content: 'Edited introduction.',
      revision: 3,
      user_revision: 1,
    })
    const finalize = vi.spyOn(api, 'finalizeLiveDraftSuggestion').mockResolvedValue(PENDING_EDIT)
    const { onFinalized } = renderPanel()

    await userEvent.clear(screen.getByRole('textbox', { name: 'Introduction draft block' }))
    await userEvent.type(
      screen.getByRole('textbox', { name: 'Introduction draft block' }),
      'Edited introduction.',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Save Introduction' }))

    await waitFor(() =>
      expect(api.updateLiveDraftSuggestionBlock).toHaveBeenCalledWith(3, 101, {
        content: 'Edited introduction.',
        expected_revision: 2,
        base_content: 'Original introduction.',
      }),
    )

    await userEvent.click(screen.getByRole('button', { name: 'Review and merge' }))

    await waitFor(() => expect(finalize).toHaveBeenCalledWith(3))
    await waitFor(() => expect(onFinalized).toHaveBeenCalledWith(PENDING_EDIT))
    expect(toast.success).toHaveBeenCalledWith('Live draft suggestion is ready for review.')
  })

  it('toasts the server conflict and keeps the panel recoverable', async () => {
    vi.spyOn(api, 'updateLiveDraftSuggestionBlock').mockRejectedValue(
      new ApiError(409, 'This block changed. Reload the live draft suggestion.'),
    )
    renderPanel()

    await userEvent.clear(screen.getByRole('textbox', { name: 'Introduction draft block' }))
    await userEvent.type(
      screen.getByRole('textbox', { name: 'Introduction draft block' }),
      'Edited introduction.',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Save Introduction' }))

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'This block changed. Reload the live draft suggestion.',
      ),
    )
  })
})
