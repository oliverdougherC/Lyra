import { expect, test } from '@playwright/test'

import { CLASS_ID, SESSION_ID, TWIN_SESSION_ID, installLyraApi } from './pla-504-math-fixture'

const document = {
  id: 101,
  class_id: CLASS_ID,
  filename: 'lecture.pdf',
  mime: 'application/pdf',
  byte_size: 1200,
  state: 'ready',
  stage_detail: null,
  pages_total: 2,
  pages_done: 2,
  pages_skipped: 0,
  pages_failed: 0,
  page_coverage: [],
  coverage_complete: true,
  refresh_state: null,
  recognize: false,
  error_message: null,
  created_at: '2026-09-28T12:00:00Z',
}

test('unsent chat and source scope survive real router history and reload', async ({ page }) => {
  await installLyraApi(page, { [`/api/classes/${CLASS_ID}/documents`]: [document] })
  await page.goto(`/#/classes/${CLASS_ID}/chat?session=${SESSION_ID}`)
  const composer = page.getByRole('textbox', { name: 'Message Lyra' })
  await composer.fill('Explain the third graph, but keep this unsent.')
  await page.getByRole('button', { name: /Choose what Lyra reads for this answer/ }).click()
  await page.getByRole('radio', { name: 'lecture.pdf' }).click()
  await expect(page.getByRole('button', { name: /Lyra reads only lecture.pdf/ })).toBeVisible()

  await page.evaluate((classId) => {
    window.location.hash = `#/classes/${classId}?tab=files`
  }, CLASS_ID)
  await expect(page.getByRole('tab', { name: 'Files' })).toBeVisible()
  await page.goBack()
  await expect(composer).toHaveValue('Explain the third graph, but keep this unsent.')
  await page.evaluate(() => sessionStorage.clear())
  await page.reload()
  await expect(composer).toHaveValue('Explain the third graph, but keep this unsent.')
  await expect(page.getByRole('button', { name: /Lyra reads only lecture.pdf/ })).toBeVisible()

  await page.evaluate(
    ({ classId, sessionId }) => {
      window.location.hash = `#/classes/${classId}/chat?session=${sessionId}`
    },
    { classId: CLASS_ID, sessionId: TWIN_SESSION_ID },
  )
  await expect(composer).toHaveValue('')
  await page.goBack()
  await expect(composer).toHaveValue('Explain the third graph, but keep this unsent.')
})

test('Class Ask retains its actual textarea through a route change and reload', async ({
  page,
}) => {
  await installLyraApi(page)
  await page.goto(`/#/classes/${CLASS_ID}`)
  const ask = page.getByRole('textbox', { name: /Ask about/ })
  await ask.fill('Help me plan the next study session.')
  await page.evaluate((classId) => {
    window.location.hash = `#/classes/${classId}?tab=files`
  }, CLASS_ID)
  await expect(page.getByRole('tab', { name: 'Files' })).toBeVisible()
  await page.goBack()
  await expect(ask).toHaveValue('Help me plan the next study session.')
  await page.evaluate(() => sessionStorage.clear())
  await page.reload()
  await expect(ask).toHaveValue('Help me plan the next study session.')
})
