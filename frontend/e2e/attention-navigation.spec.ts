import { expect, test } from '@playwright/test'

const CLASS_ID = 12

// A class of 40 documents with two that need attention. `created_at` descends with the id,
// so the list order is document 1 (newest) down to 40, and the affected documents - 7
// (failed) and 13 (unsupported) - are a few rows in, past the first visible screen.
const CREATED_BASE = new Date('2026-08-01T09:00:00Z').getTime()
const documents = Array.from({ length: 40 }, (_, i) => ({
  id: i + 1,
  class_id: CLASS_ID,
  filename: `lecture-${i + 1}.pdf`,
  mime: 'application/pdf',
  byte_size: 245760,
  state: i + 1 === 7 ? 'failed' : i + 1 === 13 ? 'unsupported' : 'ready',
  stage_detail: null,
  pages_total: 4,
  pages_done: 4,
  pages_skipped: 0,
  pages_failed: i + 1 === 7 ? 2 : 0,
  recognize: false,
  error_message:
    i + 1 === 7 ? 'Lyra could not finish reading this document. Retry it.' : null,
  created_at: new Date(CREATED_BASE - i * 60_000).toISOString(),
}))

test.beforeEach(async ({ page }) => {
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    const handlers: Record<string, unknown> = {
      '/api/classes': [
        {
          id: CLASS_ID,
          name: 'Continuous-Time Signals',
          code: 'ECE 203',
          semester: 'Spring 2026',
          archived: false,
          document_count: 40,
          created_at: '2026-01-05T09:00:00Z',
          last_active_at: '2026-08-05T09:00:00Z',
        },
      ],
      [`/api/classes/${CLASS_ID}`]: {
        id: CLASS_ID,
        name: 'Continuous-Time Signals',
        code: 'ECE 203',
        semester: 'Spring 2026',
        archived: false,
        document_count: 40,
        created_at: '2026-01-05T09:00:00Z',
        last_active_at: '2026-08-05T09:00:00Z',
      },
      [`/api/classes/${CLASS_ID}/documents`]: documents,
      [`/api/classes/${CLASS_ID}/sessions`]: [],
      [`/api/classes/${CLASS_ID}/solutions`]: [],
      [`/api/classes/${CLASS_ID}/drafts`]: [],
      [`/api/classes/${CLASS_ID}/study`]: { decks: [], quizzes: [] },
      [`/api/classes/${CLASS_ID}/profile`]: { facts: [], extraction_skipped_reason: null },
      '/api/settings': {
        endpoint_url: 'http://127.0.0.1:8080/v1',
        model: 'fixture-model',
        endpoint_is_local: true,
        api_key_set: false,
        context_window: 8192,
        remote_ack: false,
      },
      '/api/desktop-import/status': {
        available: false,
        status: 'idle',
        destination_ready: false,
      },
    }
    await route.fulfill({
      json: route.request().method() === 'POST'
        ? {}
        : (handlers[path] ?? []),
    })
  })
})

test('the overview row deep-links to the first affected document and the Files tab reveals it', async ({
  page,
}) => {
  await page.goto(`/#/classes/${CLASS_ID}`)

  // The class landing says two documents could not be used, and the link carries the
  // first affected document's stable id (list order: lecture-7 is newer than lecture-13).
  const attentionLink = page.getByRole('link', { name: /2 documents could not be used/ })
  await expect(attentionLink).toBeVisible()
  await expect(attentionLink).toHaveAttribute(
    'href',
    `/#/classes/${CLASS_ID}?tab=files&lyra-anchor=document-7`,
  )

  await attentionLink.click()

  // The click lands on the Files tab, the list scrolled to the exact row, with the
  // transient emphasis and the multi-item readout.
  await expect(
    page.locator('#main-content').getByRole('tab', { name: /Files/ }),
  ).toHaveAttribute('aria-selected', 'true')
  await expect(page.locator('#document-7')).toBeInViewport()
  await expect(page.getByText('2 need attention')).toBeVisible()
  const emphasized = await page
    .locator('#document-7')
    .evaluate((el) => el.className.includes('ring-accent-primary'))
  expect(emphasized).toBe(true)
})

test('the attention strip walks the affected documents, and Back walks them back', async ({
  page,
}) => {
  await page.goto(`/#/classes/${CLASS_ID}?tab=files&lyra-anchor=document-7`)

  await expect(page.locator('#document-7')).toBeInViewport()
  await expect(page.getByText('2 need attention')).toBeVisible()
  await expect(page.getByText(/1 of 2/)).toBeVisible()

  // Next stands on the second affected document and pushes a history entry for it.
  await page.getByRole('button', { name: 'Next document that needs attention' }).click()
  await expect(page).toHaveURL(/lyra-anchor=document-13$/)
  await expect(page.locator('#document-13')).toBeInViewport()
  await expect(page.getByText(/2 of 2/)).toBeVisible()

  // The path wraps, so a long visit never dead-ends at the last item.
  await page.getByRole('button', { name: 'Next document that needs attention' }).click()
  await expect(page).toHaveURL(/lyra-anchor=document-7$/)
  await expect(page.getByText(/1 of 2/)).toBeVisible()

  // Back re-arrives at the previously visited item, on its row.
  await page.goBack()
  await expect(page).toHaveURL(/lyra-anchor=document-13$/)
  await expect(page.locator('#document-13')).toBeInViewport()
})

test('a filter cannot hide the attention target on arrival, and Back restores the filter', async ({
  page,
}) => {
  await page.addInitScript(() => {
    sessionStorage.setItem('lyra:class:12:files-query', 'lecture-9')
  })
  await page.goto(`/#/classes/${CLASS_ID}?tab=files`)

  // The stored filter is applied on the plain Files tab, and it hides the affected row.
  const filter = page.getByRole('searchbox', { name: 'Filter documents by name' })
  await expect(filter).toHaveValue('lecture-9')
  await expect(page.locator('#document-7')).toHaveCount(0)

  // Arriving with the attention anchor clears the filter so the row can stand. The pane
  // stays mounted across this same-document navigation, which is the return the restore
  // has to serve.
  await page.evaluate((classId: number) => {
    window.location.hash = `#/classes/${classId}?tab=files&lyra-anchor=document-7`
  }, CLASS_ID)
  await expect(filter).toHaveValue('')
  await expect(page.locator('#document-7')).toBeInViewport()
  await expect(page.getByText('2 need attention')).toBeVisible()

  await page.goBack()
  await expect(page).not.toHaveURL(/lyra-anchor/)
  await expect(filter).toHaveValue('lecture-9')
  await expect(page.locator('#document-7')).toHaveCount(0)
})

test('a direct URL lands on the exact row even when it is not the first affected', async ({
  page,
}) => {
  await page.goto(`/#/classes/${CLASS_ID}?tab=files&lyra-anchor=document-13`)

  await expect(page.locator('#document-13')).toBeInViewport()
  await expect(page.getByText(/2 of 2/)).toBeVisible()
})

test('a deleted target still lands on the remaining affected documents', async ({ page }) => {
  await page.goto(`/#/classes/${CLASS_ID}?tab=files&lyra-anchor=document-99`)

  // lecture-99 no longer exists; the visit stands on the first document that still needs
  // attention instead of dying quietly, and the URL keeps naming the place it meant.
  await expect(page.locator('#document-7')).toBeInViewport()
  await expect(page).toHaveURL(/lyra-anchor=document-99$/)
})

test('an exhausted attention visit says plainly that the document is gone', async ({
  page,
}) => {
  // Every document re-ingested to ready while the link was being built.
  await page.route(`**/api/classes/${CLASS_ID}/documents`, async (route) => {
    await route.fulfill({
      json: documents.map((document) => ({ ...document, state: 'ready' })),
    })
  })
  await page.goto(`/#/classes/${CLASS_ID}?tab=files&lyra-anchor=document-99`)

  await expect(
    page.getByText('That document is no longer in this class.'),
  ).toBeVisible()
  await expect(page.locator('#documents-pane-body')).toBeVisible()
  await expect(page).toHaveURL(/lyra-anchor=document-99$/)
})
