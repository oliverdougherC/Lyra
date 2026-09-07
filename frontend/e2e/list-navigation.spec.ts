import { expect, test } from '@playwright/test'

const course = {
  id: 12,
  name: 'Continuous-Time Signals and Systems — Laboratory Methods',
  code: 'ECE 203',
  semester: 'Fall 2026',
  archived: false,
  document_count: 100,
  created_at: '2026-08-01T09:00:00Z',
  last_active_at: '2026-08-30T18:15:00Z',
}
const filename =
  'Continuous-Time-Signals-and-Systems-Laboratory-Methods-lecture-notes-final-revision-appendix-B.pdf'
const before = process.env.LISTS_BASELINE === '1'

for (const width of [375, 768, 1024, 1440]) {
  for (const theme of ['light', 'dark']) {
    test(`lists ${width} ${theme}: long names and bounded keyboard history`, async ({
      page,
    }, testInfo) => {
      const errors: string[] = []
      page.on('pageerror', (error) => errors.push(`${page.url()} ${error.stack ?? error.message}`))
      await page.setViewportSize({ width, height: 650 })
      await page.addInitScript((theme) => localStorage.setItem('lyra-theme', theme), theme)
      await page.route('**/api/**', async (route) => {
        const path = new URL(route.request().url()).pathname
        const handlers: Record<string, unknown> = {
          '/api/classes': Array.from({ length: 30 }, (_, i) => ({
            ...course,
            id: i === 0 ? 12 : i + 100,
            name: i === 0 ? course.name : `Laboratory Methods — Section ${i + 1}`,
          })),
          '/api/classes/12': course,
          '/api/classes/12/documents': Array.from({ length: 100 }, (_, i) => ({
            id: i + 1,
            class_id: 12,
            filename: i === 0 ? filename : filename.replace('appendix-B', `appendix-${i + 1}`),
            mime: 'application/pdf',
            byte_size: 245760,
            state: 'ready',
            pages_total: 4,
            pages_done: 4,
            pages_skipped: 0,
            pages_failed: 0,
            recognize: false,
            created_at: course.created_at,
          })),
          '/api/classes/12/sessions': Array.from({ length: 100 }, (_, i) => ({
            id: i + 1,
            class_id: 12,
            title: `Laboratory question ${i + 1}`,
            mode: 'guide',
            created_at: course.created_at,
          })),
          '/api/classes/12/solutions': [
            {
              id: 21,
              class_id: 12,
              title: 'Laboratory methods problem set',
              state: 'ready',
              updated_at: course.created_at,
              created_at: course.created_at,
              problems_done: 4,
              problems_total: 4,
            },
          ],
          '/api/classes/12/drafts': [],
          '/api/classes/12/study': { decks: [], quizzes: [] },
          '/api/classes/12/profile': { facts: [], extraction_skipped_reason: null },
          '/api/classes/12/workspace': null,
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
          json:
            route.request().method() === 'POST' && path.endsWith('/sessions')
              ? {
                  id: 101,
                  class_id: 12,
                  title: 'New conversation',
                  mode: 'guide',
                  created_at: course.created_at,
                }
              : (handlers[path] ?? []),
        })
      })
      await page.goto('/#/')
      await expect(page.getByRole('heading', { name: 'Classes', exact: true })).toBeVisible()
      if (!before)
        await expect(page.getByRole('button', { name: 'New class', exact: true })).toBeInViewport()
      await page.screenshot({ path: testInfo.outputPath('classes.png') })
      await page.goto('/#/classes/12?tab=files')
      const main = page.locator('#main-content')
      await expect(main.getByRole('tab', { name: /Files/ })).toHaveAttribute(
        'aria-selected',
        'true',
      )
      if (!before) {
        const name = main.getByText(filename, { exact: true })
        await expect(name).toBeVisible()
        expect(await name.evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBe(true)
        const fileViewport = page.locator('#documents-pane-body [data-slot="scroll-area-viewport"]')
        await page.screenshot({ path: testInfo.outputPath('files.png') })
        expect((await fileViewport.boundingBox())!.height).toBeGreaterThanOrEqual(150)
      }
      await page.screenshot({ path: testInfo.outputPath('files.png') })
      if (!before) {
        const filter = main.getByRole('searchbox', { name: 'Filter documents by name' })
        await filter.fill('appendix')
        await main
          .getByRole('button', { name: /^Actions for Continuous/ })
          .nth(5)
          .click()
        const viewport = page.locator('#documents-pane-body [data-slot="scroll-area-viewport"]')
        const position = await viewport.evaluate((el) => el.scrollTop)
        expect(position).toBeGreaterThan(0)
        await page.getByRole('menuitem', { name: 'Ask about this', exact: true }).click()
        await expect(page).toHaveURL(/chat/)
        await page.goBack()
        await expect(filter).toHaveValue('appendix')
        await expect.poll(() => viewport.evaluate((el) => el.scrollTop)).toBeCloseTo(position, 0)
      }
      if (width < 1024) {
        await page.getByRole('button', { name: 'Show sidebar' }).click()
      }
      const rail =
        width < 1024
          ? page.getByRole('dialog', { name: 'Sidebar' })
          : page.locator('[data-slot="sidebar-container"]')
      await expect(
        rail.getByRole('link', { name: 'Laboratory question 1', exact: true }),
      ).toBeVisible()
      await page.screenshot({ path: testInfo.outputPath('sidebar.png') })
      if (!before) {
        const find = rail.getByRole('button', { name: 'Find a conversation' })
        await find.focus()
        await page.keyboard.press('Enter')
        await page.keyboard.press('Tab')
        const search = rail.getByRole('textbox', { name: 'Search conversations' })
        await expect(search).toBeFocused()
        await search.fill('Laboratory question 99')
        await expect(
          rail.getByRole('link', { name: 'Laboratory question 99', exact: true }),
        ).toBeVisible()
        await expect(rail.getByRole('link', { name: /^Laboratory question/ })).toHaveCount(1)
        await page.screenshot({ path: testInfo.outputPath('history-search.png') })
        await search.fill('')
        await rail.getByRole('button', { name: 'Next', exact: true }).click()
        await expect(
          rail.getByRole('link', { name: 'Laboratory question 6', exact: true }),
        ).toBeVisible()
        await expect(rail.getByRole('link', { name: /^Laboratory question/ })).toHaveCount(5)
        await rail.getByRole('link', { name: 'Work', exact: true }).click()
        await expect(page).toHaveURL(/tab=work/)
        await expect(main.getByRole('tab', { name: /^Work/ })).toHaveAttribute(
          'aria-selected',
          'true',
        )
        const workSearch = main.getByRole('searchbox', { name: 'Search work by title' })
        await workSearch.fill('Laboratory question 99')
        await main.getByRole('link', { name: /Laboratory question 99/ }).click()
        await expect(page).toHaveURL(/session=99/)
        await page.goBack()
        await expect(workSearch).toHaveValue('Laboratory question 99')
        await main.getByRole('button', { name: 'Chats', exact: true }).click()
        await expect(workSearch).toHaveValue('Laboratory question 99')
        await main.getByRole('link', { name: /^Laboratory question 99 / }).click()
        await expect(page).toHaveURL(/session=99/)
        await page.goBack()
        await expect(main.getByRole('button', { name: 'Chats', exact: true })).toHaveAttribute(
          'aria-pressed',
          'true',
        )
      }
      expect(errors).toEqual([])
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
        true,
      )
    })
  }
}
