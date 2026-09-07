import { expect, test, type Page } from '@playwright/test'

// Synthetic HTTP fixtures exercise the rendered route without touching a student profile.
async function installReview(page: Page) {
  const part = (
    id: number,
    label: string,
    content: string,
    parent: number | null = null,
    page = 1,
  ) => ({
    id,
    artifact_id: 1,
    parent_part_id: parent,
    ordinal: id,
    label,
    content,
    content_type: 'markdown',
    kind: 'problem',
    status: 'pending',
    origin: 'generated',
    verdict: 'unchecked',
    verdict_detail: null,
    solve_parts: 'together',
    error_message: null,
    checks: [],
    provenance: [
      {
        chunk_id: id,
        document_id: 8,
        page_number: page,
        label: null,
        section_path: null,
        filename: 'Homework 4 — signals and systems.pdf',
        bbox: null,
      },
    ],
  })
  let solution = {
    id: 1,
    class_id: 6,
    kind: 'solution_set',
    title: 'Homework 4 — continuous-time signals and systems — corrected edition',
    state: 'awaiting_review',
    stage_detail: null,
    problems_total: 2,
    problems_done: 0,
    error_message: null,
    created_at: '2026-09-06',
    updated_at: '2026-09-06',
    sources: [
      {
        document_id: 8,
        role: 'problem_set',
        ordinal: 0,
        filename: 'Homework 4 — signals and systems.pdf',
      },
    ],
    parts: [
      part(
        10,
        'Problem 1',
        'Given the transform pair:\n\n$$e^{-2t}u(t) \\longleftrightarrow \\frac{1}{2+j\\omega}$$\n\nFind the Fourier transform of each signal.',
      ),
      part(12, '(a)', '$x(t)=e^{-2t}u(t-3)$', 10),
      part(13, '(b)', '$x(t)=t e^{-2t}u(t)$', 10),
      part(
        11,
        'Problem 2',
        'Compute the convolution of the two signals. Explain your answer and identify where the output is zero.',
        null,
        2,
      ),
    ],
  }

  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/solutions/1/segmentation') {
      const payload = route.request().postDataJSON()
      solution = {
        ...solution,
        parts: payload.problems.flatMap(
          (
            problem: {
              label: string
              statement: string
              separate_parts: boolean
              parts: { label: string; statement: string }[]
            },
            index: number,
          ) => {
            const id = 100 + index * 10
            return [
              {
                ...part(id, problem.label, problem.statement),
                solve_parts: problem.separate_parts ? 'separately' : 'together',
              },
              ...problem.parts.map((child, i) =>
                part(id + i + 1, child.label, child.statement, id),
              ),
            ]
          },
        ),
      }
      await route.fulfill({ json: solution })
      return
    }
    const cls = {
      id: 6,
      name: 'Signals and systems',
      code: 'ECE 203',
      semester: 'Fall 2026',
      archived: false,
      document_count: 1,
      created_at: '2026-09-06',
      last_active_at: '2026-09-06',
    }
    const body = path.endsWith('/study')
      ? { decks: [], quizzes: [] }
      : path === '/api/classes'
        ? [cls]
        : path === '/api/classes/6'
          ? cls
          : path === '/api/solutions/1'
            ? solution
            : path === '/api/solutions/1/status'
              ? {
                  ...solution,
                  parts: solution.parts.map((p) => ({
                    id: p.id,
                    status: p.status,
                    verdict: p.verdict,
                  })),
                }
              : path === '/api/settings'
                ? {
                    tutor_model: 'synthetic',
                    tutor_base_url: 'http://localhost:9000/v1',
                    has_api_key: true,
                  }
                : []
    await route.fulfill({ json: body })
  })
}

for (const [theme, width, height] of [
  ['light', 1280, 900],
  ['dark', 640, 620],
] as const) {
  test(`solver correction survives save and reload (${theme}, ${width}×${height})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height })
    await page.addInitScript((theme) => localStorage.setItem('lyra-theme', theme), theme)
    await installReview(page)
    await page.goto('/#/classes/6/solutions/1')
    await expect(page.getByRole('heading', { name: '2 problems found' })).toBeVisible()
    await expect(page.getByText('Page 2', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Remove part (a) of Problem 1' })).toHaveCount(0)
    await expect(page.locator('.math-text').first()).toHaveCSS('font-size', '18px')
    await expect(page.locator('h1')).toHaveText(
      'Homework 4 — continuous-time signals and systems — corrected edition',
    )

    // Enter activates the real Edit button; Tab then reaches the structural menu.
    const edit = page.getByRole('button', { name: 'Edit', exact: true }).first()
    await edit.focus()
    await page.keyboard.press('Enter')
    const child = page.getByRole('textbox', { name: 'Part 1 statement of Problem 1' })
    await child.fill('Corrected signal: $x(t)=e^{-2t}u(t-3)$')
    await page.getByRole('button', { name: 'Remove part (b) of Problem 1' }).click()
    await page.getByRole('button', { name: /Undo/ }).click()
    await expect(page.getByRole('textbox', { name: 'Part 2 statement of Problem 1' })).toHaveValue(
      '$x(t)=t e^{-2t}u(t)$',
    )
    await page.getByRole('switch').click()
    await page.getByRole('button', { name: 'Save changes' }).click()
    await expect(page.getByRole('button', { name: 'Save changes' })).toBeDisabled()
    await page.reload()
    await expect(page.getByText('Corrected signal:', { exact: false })).toBeVisible()
    await expect(page.getByRole('switch')).toBeChecked()
    await expect(
      page.getByText('Compute the convolution of the two signals.', { exact: false }),
    ).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.getByRole('button', { name: 'Solve 2 problems' }).scrollIntoViewIfNeeded()
    await expect(page.getByRole('button', { name: 'Solve 2 problems' })).toBeVisible()
  })
}
