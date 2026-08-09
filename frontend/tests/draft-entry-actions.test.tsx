import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { DraftEntryActions } from '@/components/drafts/draft-entry-actions'

describe('DraftEntryActions', () => {
  it('routes the primary button and shortcut to full-document drafting', async () => {
    const onDraftDocument = vi.fn()
    const onDraftPassage = vi.fn()
    render(<DraftEntryActions onDraftDocument={onDraftDocument} onDraftPassage={onDraftPassage} />)

    await userEvent.click(screen.getByRole('button', { name: 'Draft document' }))
    expect(onDraftDocument).toHaveBeenCalledTimes(1)
    expect(onDraftPassage).not.toHaveBeenCalled()

    await userEvent.keyboard('{Control>}/{/Control}')
    expect(onDraftDocument).toHaveBeenCalledTimes(2)
    expect(onDraftPassage).not.toHaveBeenCalled()
  })

  it('keeps the legacy one-shot tool explicitly scoped to a passage', async () => {
    const onDraftDocument = vi.fn()
    const onDraftPassage = vi.fn()
    render(<DraftEntryActions onDraftDocument={onDraftDocument} onDraftPassage={onDraftPassage} />)

    await userEvent.click(screen.getByRole('button', { name: 'Draft passage' }))

    expect(onDraftPassage).toHaveBeenCalledTimes(1)
    expect(onDraftDocument).not.toHaveBeenCalled()
  })
})
