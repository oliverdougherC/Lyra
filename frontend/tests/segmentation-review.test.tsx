import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { SegmentationReview } from '@/components/solutions/segmentation-review'
import type { SolutionDetail, SolutionPart } from '@/types'

/**
 * Contracts from docs/solver-phase-2.md: the gate exists so a missed or merged problem
 * costs one edit rather than a full re-run, and it has to say so. Merge and split are the
 * corrections that matter most here, and neither is expressible as a per-row edit, which
 * is why the whole list goes back on save.
 */

function part(overrides: Partial<SolutionPart> & { id: number }): SolutionPart {
  return {
    artifact_id: 1,
    parent_part_id: null,
    kind: 'problem',
    ordinal: 0,
    label: null,
    content: '',
    content_type: 'markdown',
    status: 'pending',
    origin: 'generated',
    verdict: 'unchecked',
    verdict_detail: null,
    solve_parts: 'together',
    error_message: null,
    provenance: [],
    checks: [],
    ...overrides,
  }
}

function solution(parts: SolutionPart[]): SolutionDetail {
  return {
    id: 1,
    class_id: 6,
    kind: 'solution_set',
    title: 'homework_4',
    state: 'awaiting_review',
    stage_detail: null,
    problems_total: parts.filter((one) => one.parent_part_id === null).length,
    problems_done: 0,
    error_message: null,
    created_at: '2026-08-03 12:00:00',
    updated_at: '2026-08-03 12:00:00',
    sources: [{ document_id: 8, role: 'problem_set', ordinal: 0, filename: 'homework_4.pdf' }],
    parts,
  }
}

const TWO_PROBLEMS = [
  part({
    id: 10,
    ordinal: 0,
    label: 'Problem 1',
    content: 'Find the transform.',
    provenance: [
      {
        chunk_id: 1,
        document_id: 8,
        page_number: 1,
        label: null,
        section_path: null,
        filename: 'homework_4.pdf',
        bbox: null,
      },
    ],
  }),
  part({ id: 11, ordinal: 1, label: 'Problem 2', content: 'Compute the convolution.' }),
]

function renderReview(parts: SolutionPart[] = TWO_PROBLEMS) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <SegmentationReview solution={solution(parts)} onResegment={vi.fn()} resegmenting={false} />
    </QueryClientProvider>,
  )
}

describe('SegmentationReview', () => {
  it('states the count and directs the student to the boundaries Lyra is least sure about', () => {
    renderReview()

    expect(screen.getByText('Lyra found 2 problems')).toBeInTheDocument()
    // The screen's job is to confirm the judgment calls (boundaries, part independence),
    // not to make the student audit every normally detected problem.
    expect(
      screen.getByText(/Lyra is least sure about the boundaries/),
    ).toBeInTheDocument()
  })

  it('keeps Save disabled until something actually changes', async () => {
    renderReview()
    const save = screen.getByRole('button', { name: 'Save changes' })
    expect(save).toBeDisabled()

    await userEvent.click(screen.getByRole('button', { name: /Actions for Problem 1/ }))
    await userEvent.click(screen.getByRole('menuitem', { name: 'Merge with next' }))

    expect(screen.getByRole('button', { name: 'Save changes' })).toBeEnabled()
  })

  it('merges a problem into the next one and marks the result edited', async () => {
    renderReview()

    await userEvent.click(screen.getByRole('button', { name: /Actions for Problem 1/ }))
    await userEvent.click(screen.getByRole('menuitem', { name: 'Merge with next' }))

    expect(screen.getByText('Lyra found 1 problem')).toBeInTheDocument()
    // Both statements survive the merge. They are two paragraphs rather than one string
    // because the preview is typeset now, and the blank line a merge inserts is what
    // keeps the two problems legible as two problems.
    expect(screen.getByText('Find the transform.')).toBeInTheDocument()
    expect(screen.getByText('Compute the convolution.')).toBeInTheDocument()
    // `Edited` means the statement is no longer verbatim from the sheet, which is exactly
    // what a merge makes true.
    expect(screen.getByText('Edited')).toBeInTheDocument()
  })

  it('cannot merge the last problem, because there is nothing after it', async () => {
    renderReview()

    await userEvent.click(screen.getByRole('button', { name: /Actions for Problem 2/ }))

    expect(screen.getByRole('menuitem', { name: 'Merge with next' })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
  })

  it('removes a problem the student says is not one', async () => {
    renderReview()

    await userEvent.click(screen.getByRole('button', { name: /Actions for Problem 2/ }))
    await userEvent.click(screen.getByRole('menuitem', { name: 'Remove' }))

    expect(screen.getByText('Lyra found 1 problem')).toBeInTheDocument()
    expect(screen.queryByText('Compute the convolution.')).not.toBeInTheDocument()
  })

  it('shows a source line only when it differs from the row above', () => {
    renderReview()

    // Eight problems from one file must not print eight identical citations, so only the
    // first row carries it.
    expect(screen.getAllByText(/homework_4\.pdf, page 1/)).toHaveLength(1)
  })

  it('offers a way forward when nothing could be segmented', () => {
    renderReview([])

    expect(screen.getByText('Lyra could not find separate problems')).toBeInTheDocument()
    // Not a dead end: some documents are prose, and that is a real outcome.
    expect(screen.getByRole('button', { name: 'Add a problem' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Read it again' })).toBeInTheDocument()
  })

  /**
   * The gate is worth exactly what can be read at it. The card used to print two clamped
   * lines of the statement, which on a signals sheet is the sentence before the
   * mathematics: the student was asked to confirm a reading of their homework with the
   * equations cut off. Nothing on this screen is truncated any more.
   */
  it('shows every problem statement in full, not a clamped preview', () => {
    const statement =
      'Starting pair:\n\n$$e^{-2t}u(t) \\longleftrightarrow \\frac{1}{2 + j\\omega}$$\n\n' +
      'Find the Fourier Transform of the following signals.'
    renderReview([part({ id: 10, ordinal: 0, label: 'Problem 1', content: statement })])

    const lead = screen.getByText('Starting pair:')
    const rendered = lead.closest('.math-text')!
    // A clamp is CSS, so the text was in the DOM either way. What the student could
    // actually see is the style on the block that holds it.
    expect(rendered.getAttribute('style')).toBeNull()
    expect(screen.getByText(/Find the Fourier Transform/)).toBeInTheDocument()
  })

  it('lists a problem sub-part under its problem and lets it be removed', async () => {
    renderReview([
      TWO_PROBLEMS[0],
      part({ id: 12, parent_part_id: 10, ordinal: 0, label: '(a)', content: 'Sketch it.' }),
    ])
    const card = screen.getByText('Sketch it.').closest('li')!

    await userEvent.click(
      within(card).getByRole('button', { name: 'Remove part (a) of Problem 1' }),
    )

    expect(screen.queryByText('Sketch it.')).not.toBeInTheDocument()
  })

  /**
   * Removing a sub-part is one click on a small X beside text the student is still
   * reading, and it used to be final: the only route back was re-reading the whole
   * sheet. Every structural edit is now reversible, and the shortcut has a button
   * beside it, because a shortcut nothing on screen mentions is a feature only its
   * author knows about.
   */
  describe('undo', () => {
    it('brings back a sub-part removed by mistake', async () => {
      renderReview([
        TWO_PROBLEMS[0],
        part({ id: 12, parent_part_id: 10, ordinal: 0, label: '(a)', content: 'Sketch it.' }),
      ])

      await userEvent.click(screen.getByRole('button', { name: 'Remove part (a) of Problem 1' }))
      expect(screen.queryByText('Sketch it.')).not.toBeInTheDocument()

      await userEvent.click(screen.getByRole('button', { name: /Undo/ }))

      expect(screen.getByText('Sketch it.')).toBeInTheDocument()
    })

    it('answers to the keyboard', async () => {
      renderReview()

      await userEvent.click(screen.getByRole('button', { name: /Actions for Problem 2/ }))
      await userEvent.click(screen.getByRole('menuitem', { name: 'Remove' }))
      expect(screen.getByText('Lyra found 1 problem')).toBeInTheDocument()

      await userEvent.keyboard('{Meta>}z{/Meta}')

      expect(screen.getByText('Lyra found 2 problems')).toBeInTheDocument()
      expect(screen.getByText('Compute the convolution.')).toBeInTheDocument()
    })

    it('leaves the shortcut to the textarea while a statement is being edited', async () => {
      renderReview()

      await userEvent.click(screen.getByRole('button', { name: /Actions for Problem 2/ }))
      await userEvent.click(screen.getByRole('menuitem', { name: 'Remove' }))
      await userEvent.click(screen.getAllByRole('button', { name: 'Edit the statement' })[0])

      const editor = screen.getByRole('textbox', { name: /statement/ })
      editor.focus()
      await userEvent.keyboard('{Meta>}z{/Meta}')

      // The removed problem stays removed: the browser's own undo owns this keystroke,
      // and taking it away would make editing a statement worse than the delete this fixes.
      expect(screen.getByText('Lyra found 1 problem')).toBeInTheDocument()
    })

    it('stays quiet until there is something to take back', () => {
      renderReview()

      expect(screen.queryByRole('button', { name: /Undo/ })).not.toBeInTheDocument()
    })
  })
})

describe('a list that arrives after the screen has mounted', () => {
  it('adopts it instead of sitting on the empty state', () => {
    // The poll flips the artifact to `awaiting_review` a moment before the detail query
    // refetches, so this screen commonly mounts with no parts at all. Seeding the draft
    // once and never again left it claiming "Lyra could not find separate problems" while
    // the backend held five.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = (parts: SolutionPart[]) => (
      <QueryClientProvider client={client}>
        <SegmentationReview solution={solution(parts)} onResegment={vi.fn()} resegmenting={false} />
      </QueryClientProvider>
    )

    const { rerender } = render(view([]))
    expect(screen.getByText('Lyra could not find separate problems')).toBeInTheDocument()

    rerender(view(TWO_PROBLEMS))

    expect(screen.getByText('Lyra found 2 problems')).toBeInTheDocument()
    expect(screen.getByText('Find the transform.')).toBeInTheDocument()
  })

  it('adopts a re-segmented list of the same length', () => {
    // A count alone would not notice this: same number of problems, different text.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = (parts: SolutionPart[]) => (
      <QueryClientProvider client={client}>
        <SegmentationReview solution={solution(parts)} onResegment={vi.fn()} resegmenting={false} />
      </QueryClientProvider>
    )

    const { rerender } = render(view(TWO_PROBLEMS))
    rerender(
      view([
        { ...TWO_PROBLEMS[0], content: 'A completely different first problem.' },
        TWO_PROBLEMS[1],
      ]),
    )

    expect(screen.getByText('A completely different first problem.')).toBeInTheDocument()
  })
})

/**
 * How a problem's parts relate is the one reading on this screen Lyra makes and the
 * student confirms rather than the other way round, so it has to be visible and it has to
 * be changeable before a minute of compute is spent on it.
 */
describe("how a problem's parts are solved", () => {
  const SECTION = [
    part({
      id: 10,
      ordinal: 0,
      label: 'Properties of LTI Systems',
      content: 'For each of the following, determine whether the system is BIBO stable.',
      solve_parts: 'separately',
    }),
    part({ id: 12, parent_part_id: 10, ordinal: 0, label: '(a)', content: 'h(t) = u(t)' }),
    part({ id: 13, parent_part_id: 10, ordinal: 1, label: '(b)', content: 'h(t) = e^-t u(t)' }),
  ]

  it('shows what Lyra read, in the words of what it decides', () => {
    renderReview(SECTION)

    const toggle = screen.getByRole('switch', {
      name: 'Solve each part of Properties of LTI Systems on its own',
    })
    expect(toggle).toBeChecked()
    expect(
      screen.getByText(/2 separate questions, each with its own answer and its own check/),
    ).toBeInTheDocument()
  })

  it('lets the student say the parts are one solution after all', async () => {
    renderReview(SECTION)

    await userEvent.click(
      screen.getByRole('switch', {
        name: 'Solve each part of Properties of LTI Systems on its own',
      }),
    )

    expect(
      screen.getByText(/Leave it this way when a part needs the answer to an earlier one/),
    ).toBeInTheDocument()
    // A reading is a change like any other on this screen: it goes back with Save.
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeEnabled()
  })

  it('says nothing about a problem with nothing to relate', () => {
    renderReview([
      TWO_PROBLEMS[0],
      part({ id: 12, parent_part_id: 10, ordinal: 0, label: '(a)', content: 'Sketch it.' }),
    ])

    // One part is not a set of questions; it is a problem whose statement runs on.
    expect(screen.queryByRole('switch')).not.toBeInTheDocument()
  })
})
