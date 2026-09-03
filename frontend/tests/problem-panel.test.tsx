import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import {
  ProblemPanel,
  type ProblemTree,
  type SubPartTree,
} from '@/components/solutions/problem-panel'
import { buildTree } from '@/components/solutions/solution-workspace'
import { Accordion } from '@/components/ui/accordion'
import { TooltipProvider } from '@/components/ui/tooltip'
import type { SolutionPart } from '@/types'

/**
 * Contracts from docs/solver-phase-2.md and docs/ui-phase-2.md:
 *
 * - A refuted solution is shown in full, with the refutation named. Hiding it would leave
 *   the student with nothing, and they may well spot the error themselves.
 * - Grounding is a count of steps carrying provenance, never a score.
 * - The audit trail is one disclosure deep from the answer, under "How Lyra checked this".
 */

function part(overrides: Partial<SolutionPart> & { id: number }): SolutionPart {
  return {
    artifact_id: 1,
    parent_part_id: null,
    kind: 'step',
    ordinal: 0,
    label: null,
    content: '',
    content_type: 'markdown',
    status: 'complete',
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

const PROBLEM = part({
  id: 1,
  kind: 'problem',
  label: 'Problem 1',
  content: 'Find the Laplace transform of a unit ramp.',
})

/** A sub-part of the question, with whatever solution it carries. */
function subPart(problem: SolutionPart, steps: SolutionPart[] = []): SubPartTree {
  return { problem, steps, answer: null }
}

function node(overrides: Partial<ProblemTree> = {}): ProblemTree {
  return {
    problem: PROBLEM,
    subParts: [],
    separate: false,
    figures: [],
    steps: [
      part({
        id: 2,
        parent_part_id: 1,
        label: 'Set up the integral',
        content: 'Apply the definition.',
        provenance: [
          {
            chunk_id: 5,
            document_id: 3,
            page_number: 12,
            label: null,
            section_path: null,
            filename: 'lecture3.pdf',
            bbox: null,
          },
        ],
      }),
      part({ id: 3, parent_part_id: 1, ordinal: 1, label: 'Evaluate', content: 'Integrate.' }),
    ],
    answer: part({ id: 4, parent_part_id: 1, kind: 'answer', ordinal: 2, content: '1/s^2' }),
    ...overrides,
  }
}

function renderPanel(tree: ProblemTree = node()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <Accordion type="multiple" defaultValue={[String(tree.problem.id)]}>
          <ProblemPanel
            node={tree}
            onAsk={vi.fn()}
            onMarkWrong={vi.fn()}
            onRegenerate={vi.fn()}
            onHistory={vi.fn()}
            onRetry={vi.fn()}
          />
        </Accordion>
      </TooltipProvider>
    </QueryClientProvider>,
  )
}

describe('ProblemPanel', () => {
  it('counts grounded steps rather than scoring them', async () => {
    renderPanel()

    // The grounding count is part of the checking surface, not the reading flow.
    await userEvent.click(screen.getByRole('button', { name: /how lyra checked this/i }))
    expect(screen.getByText('1 of 2 steps grounded in your material')).toBeInTheDocument()
    // Provenance is on the step's own row, and only on the step that has it.
    expect(screen.getByText('lecture3.pdf, page 12')).toBeInTheDocument()
  })

  it('shows a refuted solution in full and names what disagreed', () => {
    renderPanel(
      node({
        problem: {
          ...PROBLEM,
          verdict: 'refuted',
          verdict_detail: 'The integral in step 2 returns 1/s^2, the solution says 1/s.',
        },
      }),
    )

    expect(screen.getByText('Check failed')).toBeInTheDocument()
    expect(
      screen.getByText('The integral in step 2 returns 1/s^2, the solution says 1/s.'),
    ).toBeInTheDocument()
    // Still readable in full: the student may spot the error themselves.
    expect(screen.getByText('Apply the definition.')).toBeInTheDocument()
    expect(screen.getByText('1/s^2')).toBeInTheDocument()
  })

  it('lists the checks that ran under "How Lyra checked this"', async () => {
    renderPanel(
      node({
        problem: {
          ...PROBLEM,
          verdict: 'verified',
          checks: [
            {
              tool: 'cas_integrate',
              arguments: '{"expression":"t"}',
              ok: true,
              result: '{"value":"t**2/2"}',
            },
          ],
        },
      }),
    )

    // The count rides the disclosure trigger; the raw calls list on request.
    await userEvent.click(screen.getByRole('button', { name: /how lyra checked this/i }))

    expect(screen.getByText('cas_integrate')).toBeInTheDocument()
    expect(screen.getByText('{"expression":"t"}')).toBeInTheDocument()
  })

  it('offers a way forward for a problem that could not be solved', () => {
    renderPanel(
      node({
        problem: {
          ...PROBLEM,
          status: 'failed',
          error_message: 'The tutor endpoint is not reachable.',
        },
        steps: [],
        answer: null,
      }),
    )

    expect(screen.getByText('Could not be solved')).toBeInTheDocument()
    expect(screen.getByText('The tutor endpoint is not reachable.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })

  it('says what is happening while a problem is still being checked', () => {
    renderPanel(node({ problem: { ...PROBLEM, status: 'verifying' } }))

    // The Mark here would claim an outcome that has not been reached.
    expect(screen.getByText('Checking')).toBeInTheDocument()
    expect(screen.queryByText('Verified')).not.toBeInTheDocument()
  })

  it('offers re-solving only on the problem, never per step', async () => {
    renderPanel()

    await userEvent.click(screen.getByRole('button', { name: 'Actions for Problem 1' }))
    const menu = screen.getByRole('menu')

    expect(within(menu).getByRole('menuitem', { name: /Mark wrong/ })).toBeInTheDocument()
    expect(within(menu).getByRole('menuitem', { name: 'Regenerate' })).toBeInTheDocument()
    expect(within(menu).getByRole('menuitem', { name: 'History' })).toBeInTheDocument()
  })
})

describe('buildTree', () => {
  it('keeps a problem sub-part as part of the question, not of the answer', () => {
    // A sub-part is a `problem` under a `problem`: it is something to be solved. Rendering
    // it among the steps would present the question as working.
    const parts = [
      PROBLEM,
      part({ id: 10, parent_part_id: 1, kind: 'problem', label: '(a)', content: 'Sketch it.' }),
      part({ id: 11, parent_part_id: 1, kind: 'step', content: 'Apply the definition.' }),
      part({ id: 12, parent_part_id: 1, kind: 'answer', content: '1/s^2' }),
    ]

    const [tree] = buildTree(parts)

    expect(tree.subParts.map((one) => one.problem.label)).toEqual(['(a)'])
    expect(tree.steps.map((one) => one.id)).toEqual([11])
    expect(tree.answer?.id).toBe(12)
  })
})

describe('sub-part rendering', () => {
  it('does not print sub-parts the statement already contains', () => {
    // The statement is verbatim from the sheet, so it usually carries the sub-part lines
    // already. Listing them again prints every one twice.
    renderPanel(
      node({
        problem: { ...PROBLEM, content: 'Compute X.\n(a) x(t) = e^-2t\n(b) x(t) = e^-4|t|' },
        subParts: [
          subPart(
            part({
              id: 20,
              parent_part_id: 1,
              kind: 'problem',
              label: '(a)',
              content: 'x(t) = e^-2t',
            }),
          ),
          subPart(
            part({
              id: 21,
              parent_part_id: 1,
              kind: 'problem',
              label: '(b)',
              content: 'x(t) = e^-4|t|',
            }),
          ),
        ],
      }),
    )

    expect(screen.getAllByText(/x\(t\) = e\^-2t/)).toHaveLength(1)
  })

  it('prints the whole list once when one sub-part is missing from the statement', () => {
    // Dropping only the duplicates would leave a list that looks like it lost entries, so
    // the list is printed whole. The statement above it is cut back to its lead-in, so
    // printing it whole no longer means printing (a) twice.
    renderPanel(
      node({
        problem: { ...PROBLEM, content: 'Compute X.\n(a) x(t) = e^-2t' },
        subParts: [
          subPart(
            part({
              id: 20,
              parent_part_id: 1,
              kind: 'problem',
              label: '(a)',
              content: 'x(t) = e^-2t',
            }),
          ),
          subPart(
            part({
              id: 21,
              parent_part_id: 1,
              kind: 'problem',
              label: '(b)',
              content: 'Sketch it.',
            }),
          ),
        ],
      }),
    )

    expect(screen.getByText('Sketch it.')).toBeInTheDocument()
    expect(screen.getAllByText(/x\(t\) = e\^-2t/)).toHaveLength(1)
    expect(screen.getByText('Compute X.')).toBeInTheDocument()
  })
})

describe('a section whose parts are questions of their own', () => {
  const SECTION = part({
    id: 1,
    kind: 'problem',
    label: 'Properties of LTI Systems',
    content: 'For each of the following, determine whether the system is BIBO stable.',
    solve_parts: 'separately',
  })

  /** Two parts, each carrying its own working and its own verdict. */
  function section(overrides: Partial<SolutionPart>[] = [{}, {}]): ProblemTree {
    return {
      problem: SECTION,
      separate: true,
      steps: [],
      answer: null,
      figures: [],
      subParts: overrides.map((extra, index) => ({
        problem: part({
          id: 10 + index,
          parent_part_id: 1,
          kind: 'problem',
          label: `(${'ab'[index]})`,
          content: `h(t) = ${index}`,
          verdict: 'verified',
          ...extra,
        }),
        steps: [
          part({
            id: 20 + index,
            parent_part_id: 10 + index,
            label: 'Apply the criterion',
            content: `Working for part ${index}.`,
          }),
        ],
        answer: part({
          id: 30 + index,
          parent_part_id: 10 + index,
          kind: 'answer',
          content: `Answer ${index}.`,
        }),
      })),
    }
  }

  it('gives each part its own working, answer, and verdict', () => {
    renderPanel(section())

    expect(screen.getByText('Working for part 0.')).toBeInTheDocument()
    expect(screen.getByText('Answer 0.')).toBeInTheDocument()
    expect(screen.getByText('Working for part 1.')).toBeInTheDocument()
    expect(screen.getByText('Answer 1.')).toBeInTheDocument()
    // One Mark per part, and one summarising them on the section heading.
    expect(screen.getAllByText('Verified')).toHaveLength(3)
  })

  it('sums the parts into the worst verdict among them, never an average', () => {
    // Four of five passing is not a section that passed. A header calling this `Checked`
    // would claim a check that did not conclude what it appears to.
    renderPanel(section([{}, { verdict: 'refuted', verdict_detail: 'The integral diverges.' }]))

    const heading = screen.getByRole('button', { name: /Properties of LTI Systems/ })
    expect(within(heading).getByText('Check failed')).toBeInTheDocument()
    expect(screen.getByText('The integral diverges.')).toBeInTheDocument()
  })

  it('offers to re-solve one part rather than the whole section', async () => {
    const onMarkWrong = vi.fn()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <Accordion type="multiple" defaultValue={['1']}>
            <ProblemPanel
              node={section()}
              onAsk={vi.fn()}
              onMarkWrong={onMarkWrong}
              onRegenerate={vi.fn()}
              onHistory={vi.fn()}
              onRetry={vi.fn()}
            />
          </Accordion>
        </TooltipProvider>
      </QueryClientProvider>,
    )

    // The section itself has no solution to re-solve, so it carries no menu at all.
    expect(screen.queryByRole('button', { name: /Actions for Properties/ })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Actions for (b)' }))
    await userEvent.click(screen.getByRole('menuitem', { name: /Mark wrong/ }))

    expect(onMarkWrong).toHaveBeenCalledWith(expect.objectContaining({ id: 11, label: '(b)' }))
  })

  it('prints each part once, as a heading over its own answer', () => {
    // Printed as a list above and as headings below, a five-part section says the same
    // five things twice, and only the second copy carries the answers.
    renderPanel(section())

    expect(screen.getAllByText('(a)')).toHaveLength(1)
  })

  it('reads a split section out of the flat part list', () => {
    const parts = [
      SECTION,
      part({ id: 10, parent_part_id: 1, kind: 'problem', label: '(a)', content: 'h = 1' }),
      part({ id: 20, parent_part_id: 10, kind: 'step', content: 'Working.' }),
      part({ id: 30, parent_part_id: 10, kind: 'answer', content: 'Stable.' }),
    ]

    const [tree] = buildTree(parts)

    expect(tree.separate).toBe(true)
    expect(tree.steps).toEqual([])
    expect(tree.subParts[0].steps.map((one) => one.id)).toEqual([20])
    expect(tree.subParts[0].answer?.id).toBe(30)
  })

  it('is one problem until its parts have somewhere to hang their work', () => {
    // Read off the problem, not off whether the parts happen to have steps yet: a section
    // part-way through its first solve has neither, and inferring it would reshape the
    // page under the student mid-solve.
    const [tree] = buildTree([
      { ...SECTION, solve_parts: 'together' },
      part({ id: 10, parent_part_id: 1, kind: 'problem', label: '(a)', content: 'h = 1' }),
    ])

    expect(tree.separate).toBe(false)
  })
})
