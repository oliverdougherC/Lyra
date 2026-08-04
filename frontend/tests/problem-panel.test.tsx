import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { ProblemPanel, type ProblemTree } from '@/components/solutions/problem-panel'
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
 * - The audit trail travels with the badge, because it is the reason to believe it.
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

function node(overrides: Partial<ProblemTree> = {}): ProblemTree {
  return {
    problem: PROBLEM,
    subParts: [],
    steps: [
      part({
        id: 2,
        parent_part_id: 1,
        label: 'Set up the integral',
        content: 'Apply the definition.',
        provenance: [
          { chunk_id: 5, document_id: 3, page_number: 12, label: null, filename: 'lecture3.pdf' },
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
  it('counts grounded steps rather than scoring them', () => {
    renderPanel()

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

  it('states how many checks ran and lists them on request', async () => {
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

    await userEvent.click(screen.getByRole('button', { name: '1 check run' }))

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

    // A verdict badge here would claim an outcome that has not been reached.
    expect(screen.getByText('Checking')).toBeInTheDocument()
    expect(screen.queryByText('Checked')).not.toBeInTheDocument()
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

    expect(tree.subParts.map((one) => one.label)).toEqual(['(a)'])
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
          part({
            id: 20,
            parent_part_id: 1,
            kind: 'problem',
            label: '(a)',
            content: 'x(t) = e^-2t',
          }),
          part({
            id: 21,
            parent_part_id: 1,
            kind: 'problem',
            label: '(b)',
            content: 'x(t) = e^-4|t|',
          }),
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
          part({
            id: 20,
            parent_part_id: 1,
            kind: 'problem',
            label: '(a)',
            content: 'x(t) = e^-2t',
          }),
          part({ id: 21, parent_part_id: 1, kind: 'problem', label: '(b)', content: 'Sketch it.' }),
        ],
      }),
    )

    expect(screen.getByText('Sketch it.')).toBeInTheDocument()
    expect(screen.getAllByText(/x\(t\) = e\^-2t/)).toHaveLength(1)
    expect(screen.getByText('Compute X.')).toBeInTheDocument()
  })
})
