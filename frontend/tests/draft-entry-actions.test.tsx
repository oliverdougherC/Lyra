import { renderHook } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { useDraftDocumentShortcut } from '@/components/drafts/draft-entry-actions'

describe('useDraftDocumentShortcut', () => {
  it('routes Ctrl/Cmd + / to full-document drafting and nothing else', async () => {
    const onDraftDocument = vi.fn()
    renderHook(() => useDraftDocumentShortcut(onDraftDocument))

    // A bare modifier press is not the shortcut.
    await userEvent.keyboard('{Control>}')
    expect(onDraftDocument).not.toHaveBeenCalled()

    // Nor is a plain "/".
    await userEvent.keyboard('/')
    expect(onDraftDocument).not.toHaveBeenCalled()

    await userEvent.keyboard('{Control>}/{/Control}')
    expect(onDraftDocument).toHaveBeenCalledTimes(1)

    // The macOS flavor of the same gesture.
    await userEvent.keyboard('{Meta>}/{/Meta}')
    expect(onDraftDocument).toHaveBeenCalledTimes(2)
  })
})
