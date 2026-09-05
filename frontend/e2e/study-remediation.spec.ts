import { expect, test, type Locator, type Page } from '@playwright/test'

const cardState = {
  due_at: '2026-08-06 12:00:00',
  stability: 0,
  difficulty: 5,
  reps: 0,
  lapses: 0,
  state: 'new',
  last_review_at: null,
  bucket: 'new',
}
const question = 'What does $x^2$ mean?'
const answer = `${'Multiply x by itself. '.repeat(60)}End of the long answer.`

async function installStudy(page: Page) {
  const course = {
    id: 12,
    name: 'Mathematics',
    code: 'MATH',
    semester: 'Fall 2026',
    archived: false,
    document_count: 0,
    created_at: '2026-08-01T09:00:00Z',
  }
  const deck = {
    id: 41,
    class_id: 12,
    kind: 'flashcard_deck',
    title: 'Practice',
    state: 'ready',
    stage_detail: null,
    problems_total: null,
    problems_done: 0,
    error_message: null,
    cards_total: 1,
    due_count: 1,
    buckets: { new: 1, learning: 0, mastered: 0 },
  }
  const cards = [
    {
      part_id: 11,
      label: null,
      card: { front: question, back: answer, topic: 'Powers' },
      due: true,
      card_state: cardState,
    },
  ]
  const responses: Record<string, unknown> = {
    '/api/classes': [course],
    '/api/classes/12': course,
    '/api/classes/12/study': { decks: [deck], quizzes: [] },
    '/api/decks/41/status': deck,
    '/api/decks/41/session': { cards },
    '/api/decks/41': { ...deck, cards },
    '/api/settings': {
      endpoint_url: 'http://127.0.0.1:8080/v1',
      endpoint_is_local: true,
      endpoint_host: '127.0.0.1',
      model: 'fixture-model',
      context_window: 8192,
      extraction_enabled: true,
      api_key_set: false,
      remote_ack: false,
    },
    '/api/desktop-import/status': { available: false, status: 'idle' },
  }
  await page.route('http://127.0.0.1:8000/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(responses[path] ?? []),
    })
  })
}

async function expectUnobscured(control: Locator, page: Page) {
  await expect(control).toBeVisible()
  const box = await control.boundingBox()
  const navigation = await page.getByRole('navigation', { name: 'Mobile navigation' }).boundingBox()
  expect(box).not.toBeNull()
  expect(navigation).not.toBeNull()
  expect(box!.y).toBeGreaterThanOrEqual(0)
  expect(box!.y + box!.height).toBeLessThanOrEqual(navigation!.y)
  expect(box!.x).toBeGreaterThanOrEqual(0)
  expect(box!.x + box!.width).toBeLessThanOrEqual(page.viewportSize()!.width)
  expect(
    await control.evaluate((element) => {
      const rect = element.getBoundingClientRect()
      return element.contains(
        document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2),
      )
    }),
  ).toBe(true)
}

for (const viewport of [
  { width: 375, height: 667 },
  // Same CSS layout area as a 640x960 browser window at 200% zoom. This checks
  // reflow/clearance; it does not claim to emulate browser text rasterization.
  { width: 320, height: 480 },
]) {
  test.describe(`flashcard recovery at ${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport, hasTouch: true, isMobile: true })

    test('keeps readable math, long answers and touch ratings outside bottom navigation', async ({
      page,
    }) => {
      await installStudy(page)
      await page.goto('/#/classes/12/study/41')
      expect(await page.evaluate(() => matchMedia('(hover: none)').matches)).toBe(true)
      const front = page.getByRole('region', { name: 'Card question', exact: true })
      await expect(front).toBeVisible()
      await expect(front.locator('math')).toHaveCount(1)
      await expect(front.locator('button')).toHaveCount(0)
      await expect(page.getByRole('region', { name: 'Card answer', exact: true })).toHaveCount(0)
      const actions = page.getByRole('button', { name: 'Card actions', exact: true })
      await expect(actions).toHaveCSS('opacity', '1')
      await actions.tap()
      await expect(page.getByRole('menuitem', { name: 'Edit card' })).toBeVisible()
      await page.keyboard.press('Escape')
      await page.getByRole('button', { name: 'Show answer', exact: true }).tap()
      const back = page.getByRole('region', { name: 'Card answer', exact: true })
      await expect(back).toBeFocused()
      await expect(front).toHaveCount(0)
      expect(await back.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(
        true,
      )
      await back.evaluate((element) => {
        element.scrollTop = element.scrollHeight
      })
      await expect(back).toContainText('End of the long answer.')
      const ratings = page.getByRole('group', { name: 'Rate this card' })
      // At the original reported viewport every rating must fit on initial reveal.
      if (viewport.width === 375) {
        await page.screenshot({ path: '/tmp/lyra-pla404-recovery/study-narrow.png' })
        for (const name of ['Again', 'Hard', 'Good', 'Easy']) {
          await expectUnobscured(ratings.getByRole('button', { name: new RegExp(name) }), page)
        }
      }
      // At the compact zoom-equivalent size scrolling must expose the complete row,
      // with no fixed-navigation overlay or horizontal page overflow.
      await page.locator('main').evaluate((element) => {
        element.scrollTop = element.scrollHeight
      })
      for (const name of ['Again', 'Hard', 'Good', 'Easy']) {
        await expectUnobscured(ratings.getByRole('button', { name: new RegExp(name) }), page)
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
        viewport.width,
      )
    })
  })
}
