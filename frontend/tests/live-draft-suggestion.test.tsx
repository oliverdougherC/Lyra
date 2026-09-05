import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
  it('saves every dirty paragraph before preparing review and keeps failed edits recoverable', async () => {
    const save = vi
      .spyOn(api, 'updateLiveDraftSuggestionBlock')
      .mockImplementation(async (_id, blockId, body) => ({
        ...LIVE_SUGGESTION.blocks.find((block) => block.id === blockId)!,
        content: body.content,
        revision: 6,
      }))
    const finalize = vi.spyOn(api, 'finalizeLiveDraftSuggestion').mockResolvedValue(PENDING_EDIT)
    renderPanel()
    fireEvent.change(screen.getByLabelText('Introduction draft block'), {
      target: { value: 'New intro' },
    })
    fireEvent.change(screen.getByLabelText('Claims draft block'), {
      target: { value: 'New claims' },
    })
    await userEvent.click(screen.getByRole('button', { name: 'Review and merge' }))
    await waitFor(() => expect(finalize).toHaveBeenCalledOnce())
    expect(save).toHaveBeenCalledTimes(2)
    expect(
      save.mock.invocationCallOrder.every((order) => order < finalize.mock.invocationCallOrder[0]),
    ).toBe(true)
  })

  it('does not finalize when saving a dirty paragraph fails', async () => {
    vi.spyOn(api, 'updateLiveDraftSuggestionBlock').mockRejectedValue(new Error('Offline'))
    const finalize = vi.spyOn(api, 'finalizeLiveDraftSuggestion').mockResolvedValue(PENDING_EDIT)
    renderPanel()
    fireEvent.change(screen.getByLabelText('Introduction draft block'), {
      target: { value: 'Keep this writing' },
    })
    await userEvent.click(screen.getByRole('button', { name: 'Review and merge' }))
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
    expect(finalize).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Introduction draft block')).toHaveValue('Keep this writing')
    expect(screen.getByRole('button', { name: 'Save Introduction' })).toBeEnabled()
  })

  it('preserves later typing and flushes it after an outstanding save before finalizing', async () => {
    let resolveSave!: (block: (typeof LIVE_SUGGESTION.blocks)[number]) => void
    const save = vi
      .spyOn(api, 'updateLiveDraftSuggestionBlock')
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveSave = resolve
          }),
      )
      .mockImplementation(async (_id, blockId, body) => ({
        ...LIVE_SUGGESTION.blocks.find((block) => block.id === blockId)!,
        content: body.content,
        revision: 4,
      }))
    const finalize = vi.spyOn(api, 'finalizeLiveDraftSuggestion').mockResolvedValue(PENDING_EDIT)
    renderPanel()
    const input = screen.getByLabelText('Introduction draft block')
    fireEvent.change(input, { target: { value: 'First edit' } })
    await userEvent.click(screen.getByRole('button', { name: 'Save Introduction' }))
    fireEvent.change(input, { target: { value: 'First edit and later typing' } })
    await userEvent.click(screen.getByRole('button', { name: 'Review and merge' }))
    expect(finalize).not.toHaveBeenCalled()
    await act(async () =>
      resolveSave({ ...LIVE_SUGGESTION.blocks[0], content: 'First edit', revision: 3 }),
    )
    await waitFor(() => expect(finalize).toHaveBeenCalledOnce())
    expect(input).toHaveValue('First edit and later typing')
    expect(save).toHaveBeenLastCalledWith(3, 101, {
      content: 'First edit and later typing',
      expected_revision: 3,
      base_content: 'First edit',
    })
  })
  it('keeps later typing marked unsaved after a manual save completes', async () => {
    let resolveSave!: (block: (typeof LIVE_SUGGESTION.blocks)[number]) => void
    vi.spyOn(api, 'updateLiveDraftSuggestionBlock').mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveSave = resolve
        }),
    )
    renderPanel()
    const input = screen.getByLabelText('Introduction draft block')
    fireEvent.change(input, { target: { value: 'Submitted text' } })
    await userEvent.click(screen.getByRole('button', { name: 'Save Introduction' }))
    fireEvent.change(input, { target: { value: 'Submitted text plus later writing' } })
    await act(async () =>
      resolveSave({ ...LIVE_SUGGESTION.blocks[0], content: 'Submitted text', revision: 3 }),
    )
    expect(input).toHaveValue('Submitted text plus later writing')
    expect(screen.getByRole('button', { name: 'Save Introduction' })).toBeEnabled()
  })
  it('recovers the confirmed base after a successful PATCH response was lost', async () => {
    const saved = {
      ...LIVE_SUGGESTION.blocks[0],
      content: 'Saved replacement',
      revision: 3,
      user_revision: 1,
    }
    const patch = vi
      .spyOn(api, 'updateLiveDraftSuggestionBlock')
      .mockRejectedValueOnce(new Error('Response lost'))
      .mockImplementation(async (_id, _block, body) => ({
        ...saved,
        content: body.content,
        revision: 4,
      }))
    vi.spyOn(api, 'getLiveDraftSuggestion').mockResolvedValue({
      ...LIVE_SUGGESTION,
      blocks: [saved, LIVE_SUGGESTION.blocks[1]],
    })
    renderPanel()
    const input = screen.getByLabelText('Introduction draft block')
    fireEvent.change(input, { target: { value: 'Saved replacement' } })
    await userEvent.click(screen.getByRole('button', { name: 'Save Introduction' }))
    await screen.findByRole('alert')
    fireEvent.change(input, { target: { value: 'Saved replacement plus later typing' } })
    await userEvent.click(screen.getByRole('button', { name: 'Save Introduction' }))
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(2))
    expect(patch).toHaveBeenLastCalledWith(3, 101, {
      content: 'Saved replacement plus later typing',
      expected_revision: 3,
      base_content: 'Saved replacement',
    })
    expect(input).toHaveValue('Saved replacement plus later typing')
  })

  it('keeps a concurrently streamed suffix alongside typing made during a save', async () => {
    let resolveSave!: (block: (typeof LIVE_SUGGESTION.blocks)[number]) => void
    const patch = vi
      .spyOn(api, 'updateLiveDraftSuggestionBlock')
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveSave = resolve
          }),
      )
      .mockImplementation(async (_id, _block, body) => ({
        ...LIVE_SUGGESTION.blocks[0],
        content: body.content,
        revision: 5,
      }))
    renderPanel()
    const input = screen.getByLabelText('Introduction draft block')
    fireEvent.change(input, { target: { value: 'User replacement' } })
    await userEvent.click(screen.getByRole('button', { name: 'Save Introduction' }))
    fireEvent.change(input, { target: { value: 'User replacement and later typing' } })
    await act(async () =>
      resolveSave({
        ...LIVE_SUGGESTION.blocks[0],
        content: 'User replacement MODEL CONTINUATION',
        revision: 4,
        user_revision: 1,
      }),
    )
    expect(input).toHaveValue('User replacement and later typing MODEL CONTINUATION')
    await userEvent.click(screen.getByRole('button', { name: 'Save Introduction' }))
    expect(patch).toHaveBeenLastCalledWith(3, 101, {
      content: 'User replacement and later typing MODEL CONTINUATION',
      expected_revision: 4,
      base_content: 'User replacement MODEL CONTINUATION',
    })
  })
  it('can save a return to the original text after an uncertain replacement landed', async () => {
    const saved = {
      ...LIVE_SUGGESTION.blocks[0],
      content: 'Saved replacement',
      revision: 3,
      user_revision: 1,
    }
    const patch = vi
      .spyOn(api, 'updateLiveDraftSuggestionBlock')
      .mockRejectedValueOnce(new Error('Response lost'))
      .mockImplementation(async (_id, _block, body) => ({
        ...saved,
        content: body.content,
        revision: 4,
      }))
    vi.spyOn(api, 'getLiveDraftSuggestion').mockResolvedValue({
      ...LIVE_SUGGESTION,
      blocks: [saved, LIVE_SUGGESTION.blocks[1]],
    })
    renderPanel()
    const input = screen.getByLabelText('Introduction draft block')
    fireEvent.change(input, { target: { value: 'Saved replacement' } })
    await userEvent.click(screen.getByRole('button', { name: 'Save Introduction' }))
    await screen.findByRole('alert')
    fireEvent.change(input, { target: { value: 'Original introduction.' } })
    expect(screen.getByRole('button', { name: 'Save Introduction' })).toBeEnabled()
    await userEvent.click(screen.getByRole('button', { name: 'Save Introduction' }))
    await waitFor(() =>
      expect(patch).toHaveBeenLastCalledWith(3, 101, {
        content: 'Original introduction.',
        expected_revision: 3,
        base_content: 'Saved replacement',
      }),
    )
  })
  it('does not mistake a model append for an acknowledged failed replacement', async () => {
    const confirmed = {
      ...LIVE_SUGGESTION.blocks[0],
      content: 'Saved replacement',
      revision: 3,
      user_revision: 1,
    }
    const patch = vi
      .spyOn(api, 'updateLiveDraftSuggestionBlock')
      .mockResolvedValueOnce(confirmed)
      .mockRejectedValueOnce(new Error('Offline before write'))
      .mockResolvedValueOnce({
        ...confirmed,
        content: 'Saved MODEL',
        revision: 5,
        user_revision: 2,
      })
    vi.spyOn(api, 'getLiveDraftSuggestion').mockResolvedValue({
      ...LIVE_SUGGESTION,
      blocks: [
        { ...confirmed, content: 'Saved replacement MODEL', revision: 4 },
        LIVE_SUGGESTION.blocks[1],
      ],
    })
    renderPanel()
    const input = screen.getByLabelText('Introduction draft block')
    fireEvent.change(input, { target: { value: 'Saved replacement' } })
    await userEvent.click(screen.getByRole('button', { name: 'Save Introduction' }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save Introduction' })).toBeDisabled(),
    )
    fireEvent.change(input, { target: { value: 'Saved' } })
    await userEvent.click(screen.getByRole('button', { name: 'Save Introduction' }))
    await screen.findByRole('alert')
    await userEvent.click(screen.getByRole('button', { name: 'Save Introduction' }))
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(3))
    expect(patch).toHaveBeenLastCalledWith(3, 101, {
      content: 'Saved',
      expected_revision: 3,
      base_content: 'Saved replacement',
    })
    expect(input).toHaveValue('Saved MODEL')
  })
})
