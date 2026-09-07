import { expect, test, type Page, type Route } from '@playwright/test'

type RouteHandler = (route: Route) => Promise<void>

async function installApiMocks(page: Page) {
  const seenRequests: string[] = []
  const unexpectedRequests: string[] = []
  const handlers = new Map<string, RouteHandler>([
    [
      '/api/classes',
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: '[]',
        })
      },
    ],
    [
      '/api/settings',
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            endpoint_url: 'http://127.0.0.1:8080/v1',
            api_key_set: false,
            api_key_storage: 'file',
            model: null,
            context_window: 8192,
            extraction_enabled: true,
            remote_ack: false,
            endpoint_is_local: true,
            endpoint_host: '127.0.0.1',
            embedding_model: null,
            embedding_dim: null,
            tools_supported: null,
            tools_message: null,
            vision_supported: null,
            vision_message: null,
            allow_web_research: false,
            parallel_requests: true,
            parallel_concurrency: 2,
            exa_api_key_set: false,
            exa_api_key_storage: 'file',
          }),
        })
      },
    ],
    [
      '/api/desktop-import/status',
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            available: false,
            destination_ready: false,
            status: 'idle',
            phase: null,
            message: null,
            source_name: null,
            copied_entries: 0,
            total_entries: 0,
            copied_bytes: 0,
            total_bytes: 0,
            cancel_requested: false,
            can_resume: false,
            requires_restart: false,
            preview: null,
          }),
        })
      },
    ],
  ])

  await page.route('http://127.0.0.1:8000/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    seenRequests.push(pathname)
    const handler = handlers.get(pathname)
    if (handler) {
      await handler(route)
      return
    }
    unexpectedRequests.push(pathname)
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ detail: `unexpected API call in smoke test: ${pathname}` }),
    })
  })

  return { seenRequests, unexpectedRequests }
}

test.describe('nonvisual browser smoke', () => {
  test('renders the classes home page without API or runtime errors', async ({ page }) => {
    const api = await installApiMocks(page)
    const pageErrors: string[] = []
    page.on('pageerror', (error) => pageErrors.push(error.message))

    await page.goto('/#/')
    await page.waitForLoadState('networkidle')

    await expect(page.getByRole('heading', { name: 'Classes' })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Settings' }).first()).toBeVisible()
    expect(api.unexpectedRequests).toEqual([])
    expect(pageErrors).toEqual([])
  })

  test('renders the settings page from mocked local configuration', async ({ page }) => {
    const api = await installApiMocks(page)
    const pageErrors: string[] = []
    page.on('pageerror', (error) => pageErrors.push(error.message))

    await page.goto('/#/')
    await page.getByRole('link', { name: 'Settings' }).first().click()
    await page.waitForLoadState('networkidle')

    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
    expect(api.unexpectedRequests).toEqual([])
    expect(pageErrors).toEqual([])
  })

  test('keeps the current route when a selected citation is encoded into the route URL, including back/forward and reload', async ({
    page,
  }) => {
    const api = await installApiMocks(page)
    const pageErrors: string[] = []
    page.on('pageerror', (error) => pageErrors.push(error.message))

    await page.goto('/#/')
    await page.waitForLoadState('networkidle')
    await page.getByRole('link', { name: 'Settings' }).first().click()
    await page.waitForLoadState('networkidle')
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()

    await page.evaluate(() => {
      window.location.hash = '#/settings?tab=advanced'
    })
    await expect(page).toHaveURL(/#\/settings\?tab=advanced$/)

    await page.evaluate(() => {
      window.location.hash = '#/settings?tab=advanced&lyra-anchor=source-17'
    })

    await expect(page).toHaveURL(/#\/settings\?tab=advanced&lyra-anchor=source-17$/)
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()

    await page.goBack()
    await expect(page).toHaveURL(/#\/settings\?tab=advanced$/)
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()

    await page.goForward()
    await expect(page).toHaveURL(/#\/settings\?tab=advanced&lyra-anchor=source-17$/)
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()

    await page.reload()
    await page.waitForLoadState('networkidle')
    await expect(page).toHaveURL(/#\/settings\?tab=advanced&lyra-anchor=source-17$/)
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()

    expect(api.unexpectedRequests).toEqual([])
    expect(pageErrors).toEqual([])
  })
})

test('restores the inner Settings reading position across Back, Forward and reload', async ({
  page,
}) => {
  await installApiMocks(page)
  await page.setViewportSize({ width: 1024, height: 600 })
  await page.goto('/#/settings')
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
  const main = page.locator('#main-content')
  await main.evaluate((element) => {
    element.scrollTop = 380
  })
  await expect.poll(() => main.evaluate((element) => element.scrollTop)).toBe(380)
  await page.locator('a[href="/#/"]').first().click()
  await expect(page.getByRole('heading', { name: 'Classes' })).toBeVisible()
  await expect.poll(() => main.evaluate((element) => element.scrollTop)).toBe(0)
  await page.goBack()
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
  await expect.poll(() => main.evaluate((element) => element.scrollTop)).toBe(380)
  await page.reload()
  await expect.poll(() => main.evaluate((element) => element.scrollTop)).toBe(380)
  await page.goForward()
  await expect(page.getByRole('heading', { name: 'Classes' })).toBeVisible()
  await expect.poll(() => main.evaluate((element) => element.scrollTop)).toBe(0)
})
