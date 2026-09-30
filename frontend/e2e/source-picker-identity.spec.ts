import { expect, test, type Locator } from '@playwright/test'

import { CLASS_ID, SESSION_ID, installLyraApi } from './pla-504-math-fixture'

const createdAt = '2026-08-31T12:00:00Z'
const longPrefix = 'LADW_2026_08-31_'.repeat(5)
const documents = [
  { id: 7, filename: 'LADW_2026_08-31.pdf', nickname: 'Textbook' },
  { id: 8, filename: 'LADW_2026_08-31.pdf', nickname: 'Textbook' },
  { id: 9, filename: `${longPrefix}chapter-A.pdf`, nickname: 'Reading' },
  { id: 10, filename: `${longPrefix}chapter-B.pdf`, nickname: 'Reading' },
  { id: 11, filename: 'unique.pdf', nickname: 'Notes' },
].map((item) => ({
  ...item,
  class_id: CLASS_ID,
  mime: 'application/pdf',
  byte_size: 2048,
  state: 'ready',
  stage_detail: null,
  pages_total: 2,
  pages_done: 2,
  pages_skipped: 0,
  pages_failed: 0,
  recognize: false,
  error_message: null,
  created_at: createdAt,
}))

/** Measure the rendered text itself, including clipping by the row and picker. */
async function expectVisibleId(row: Locator, id: number) {
  await row.scrollIntoViewIfNeeded()
  const badge = row.getByText(`#${id}`, { exact: true })
  await expect(badge).toBeVisible()
  const geometry = await badge.evaluate((element) => {
    const range = document.createRange()
    range.selectNodeContents(element)
    const text = range.getBoundingClientRect()
    const row = element.closest('label')!.getBoundingClientRect()
    const panel = element.closest('[data-slot="popover-content"]')!.getBoundingClientRect()
    const centerX = text.left + text.width / 2
    const centerY = text.top + text.height / 2
    return {
      textWidth: text.width,
      inRow: text.left >= row.left && text.right <= row.right,
      inPanel: text.left >= panel.left && text.right <= panel.right,
      inViewport: centerX >= 0 && centerX < innerWidth && centerY >= 0 && centerY < innerHeight,
      hit: element.contains(document.elementFromPoint(centerX, centerY)),
    }
  })
  expect(geometry, `Document #${id} must be readable before selection`).toMatchObject({
    inRow: true,
    inPanel: true,
    inViewport: true,
    hit: true,
  })
  expect(geometry.textWidth).toBeGreaterThan(10)
}

test('colliding source identities remain visible and selection stays scoped through search and refetch', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 640, height: 700 })
  await installLyraApi(page)
  let current = documents
  await page.route(`**/api/classes/${CLASS_ID}/documents`, (route) =>
    route.fulfill({ contentType: 'application/json', body: JSON.stringify(current) }),
  )
  await page.goto(`/#/classes/${CLASS_ID}/chat?session=${SESSION_ID}`)

  const chip = page.getByRole('button', { name: /Choose what Lyra reads for this answer/ })
  const panel = page.locator('[data-slot="popover-content"]')
  const choice = (id: number) => panel.locator(`[role="radio"][id="all-${id}"]`)
  const open = () => chip.click()

  await open()
  await expectVisibleId(choice(7).locator('..'), 7)
  await expectVisibleId(choice(8).locator('..'), 8)
  await expectVisibleId(choice(9).locator('..'), 9)
  await expectVisibleId(choice(10).locator('..'), 10)
  await expect(
    panel.getByRole('radio', { name: 'Notes' }).locator('..').getByText('#11'),
  ).toHaveCount(0)
  await testInfo.attach('source-picker-collision-rows', {
    body: await panel.screenshot(),
    contentType: 'image/png',
  })

  const search = panel.getByRole('textbox', { name: "Search this class's files" })
  await search.fill('LADW_2026_08-31.pdf')
  await expect(choice(7)).toBeVisible()
  await expect(choice(8)).toBeVisible()
  await choice(7).click()
  await expect(chip).toContainText('#7')

  current = [...documents].reverse()
  await page.reload()
  await expect(chip).toContainText('#7')
  await open()
  await expectVisibleId(choice(8).locator('..'), 8)
  await choice(8).click()
  await expect(chip).toContainText('#8')

  await open()
  await search.fill('chapter-A.pdf')
  await expect(choice(9)).toBeVisible()
  await expect(choice(10)).toHaveCount(0)
  await expectVisibleId(choice(9).locator('..'), 9)
  await choice(9).click()

  await open()
  await expect(choice(9)).toBeChecked()
  await search.fill('chapter-B.pdf')
  await expectVisibleId(choice(10).locator('..'), 10)
  await choice(10).click()
  await open()
  await expect(choice(10)).toBeChecked()
  await page.keyboard.press('Escape')

  const request = page.waitForRequest(
    (candidate) => candidate.url().endsWith('/agent-chat') && candidate.method() === 'POST',
  )
  await page.getByRole('textbox', { name: 'Message Lyra' }).fill('Explain this reading.')
  await page.getByRole('button', { name: 'Send message' }).click()
  expect((await request).postDataJSON()).toHaveProperty('document_id', 10)
})
