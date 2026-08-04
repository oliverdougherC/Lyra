import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { SourcePicker } from '@/components/solutions/source-picker'
import type { DocumentRead, DocumentState } from '@/types'

function document(id: number, filename: string, state: DocumentState = 'ready'): DocumentRead {
  return {
    id,
    class_id: 1,
    filename,
    mime: 'application/pdf',
    byte_size: 2048,
    state,
    stage_detail: null,
    pages_total: 3,
    pages_done: 3,
    pages_skipped: 0,
    error_message: null,
    created_at: '2026-08-04 00:00:00',
  }
}

const documents = [document(1, 'homework_5.pdf'), document(2, 'scan.pdf', 'unsupported')]

describe('SourcePicker', () => {
  it('keeps its hidden checkboxes inside their own row', () => {
    // The bug this guards against had no visible cause and a spectacular symptom. The
    // checkbox is `sr-only`, which is `position: absolute`, and an absolutely positioned
    // box is clipped only by ancestors between it and its containing block. With no
    // positioned parent on the row, that containing block was the app shell's inset --
    // above the scroll container and itself unscrollable by hand. Clicking a row focused
    // a checkbox the browser believed to be off-screen, so it scrolled the inset, and the
    // page slid away with no scrollbar to bring it back.
    //
    // jsdom does no layout, so this asserts the structure that makes the layout correct
    // rather than the layout itself: the row must establish the containing block.
    render(
      <SourcePicker
        name="problem-set"
        documents={documents}
        loading={false}
        selected={[]}
        onToggle={vi.fn()}
        emptyLabel="Nothing here."
      />,
    )

    const checkbox = screen.getByRole('checkbox', { name: /homework_5/ })
    const row = checkbox.closest('label')

    expect(checkbox).toHaveClass('sr-only')
    expect(row).toHaveClass('relative')
  })

  it('reports a document it cannot read instead of hiding it', () => {
    render(
      <SourcePicker
        name="problem-set"
        documents={documents}
        loading={false}
        selected={[]}
        onToggle={vi.fn()}
        emptyLabel="Nothing here."
      />,
    )

    expect(screen.getByText('scan.pdf')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /scan/ })).toBeDisabled()
  })

  it('does not offer a document the other picker has taken', async () => {
    const onToggle = vi.fn()
    render(
      <SourcePicker
        name="reference-solutions"
        documents={documents}
        loading={false}
        selected={[]}
        claimed={[1]}
        onToggle={onToggle}
        emptyLabel="Nothing here."
      />,
    )

    const checkbox = screen.getByRole('checkbox', { name: /homework_5/ })
    expect(checkbox).toBeDisabled()
    expect(screen.getByText('Already used above')).toBeInTheDocument()

    await userEvent.click(checkbox)
    expect(onToggle).not.toHaveBeenCalled()
  })
})
