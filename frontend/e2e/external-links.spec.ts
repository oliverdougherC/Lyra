import { expect, test, type Page, type Route } from '@playwright/test'

type RouteHandler = (route: Route) => Promise<void>

declare global {
  interface Window {
    __TAURI_INTERNALS__?: {
      invoke?: (command: string, args?: Record<string, unknown>) => Promise<unknown>
    }
    __lyraExternalOpens__?: Array<{ command: string; args: Record<string, unknown> | undefined }>
    __lyraRejectExternalOpen__?: boolean
  }
}

const TAURI_BOOTSTRAP_PAYLOAD = {
  protocolVersion: 1,
  apiBase: 'http://127.0.0.1:8000',
  sessionHeaderName: 'X-Lyra-Session',
  sessionSecret: 'a'.repeat(64),
}

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
      body: JSON.stringify({ detail: `unexpected API call in external link test: ${pathname}` }),
    })
  })

  return { seenRequests, unexpectedRequests }
}

async function installTauriInvokeMock(page: Page) {
  await page.addInitScript((bootstrapPayload) => {
    window.__lyraExternalOpens__ = []
    window.__lyraRejectExternalOpen__ = false
    window.__TAURI_INTERNALS__ = {
      invoke: async (command: string, args?: Record<string, unknown>) => {
        if (command === 'desktop_bootstrap' || command === 'retry_backend') {
          return bootstrapPayload
        }
        if (command !== 'open_external_url') {
          throw new Error(`unexpected Tauri command in external-links spec: ${command}`)
        }
        window.__lyraExternalOpens__?.push({ command, args })
        if (window.__lyraRejectExternalOpen__) {
          throw new Error('mocked open failure')
        }
        return null
      },
    }
  }, TAURI_BOOTSTRAP_PAYLOAD)
}

async function mountExternalLinkFixture(page: Page) {
  await page.evaluate(() => {
    const root = document.createElement('section')
    root.setAttribute('aria-label', 'External link fixture')
    root.setAttribute(
      'style',
      [
        'position:fixed',
        'top:16px',
        'right:16px',
        'z-index:2147483647',
        'padding:12px',
        'display:flex',
        'flex-direction:column',
        'gap:8px',
        'background:white',
        'color:black',
        'border:1px solid black',
      ].join(';'),
    )
    root.innerHTML = `
      <button type="button" id="focus-anchor">Keep focus here</button>
      <div class="assistant-content">
        <a href="https://example.com/docs">Model markdown link</a>
        <a href="https://example.com/paper" target="_blank" rel="noreferrer">Target blank link</a>
        <a href="http://127.0.0.1:8000/admin">Unsafe loopback link</a>
      </div>
    `
    document.body.appendChild(root)
  })
}

async function readOpenCalls(page: Page) {
  return page.evaluate(() => window.__lyraExternalOpens__ ?? [])
}

/**
 * Rust unit tests cover the packaged Tauri boundary itself:
 * `on_navigation`, `on_new_window`, URL validation, and DNS revalidation.
 * This spec covers renderer behavior in the browser engine by mocking the Tauri invoke seam,
 * so it can assert interception, rejection, and route preservation without real browser side effects.
 *
 * Chromium remains the default CI project. WebKit is opt-in via `PLAYWRIGHT_ENABLE_WEBKIT=1`.
 */
test.describe('external link interception', () => {
  test('same-window markdown links route through the typed opener command and keep the current route', async ({
    page,
  }) => {
    const api = await installApiMocks(page)
    await installTauriInvokeMock(page)
    await page.goto('/#/')
    await page.waitForLoadState('networkidle')
    await expect(page.getByRole('heading', { name: 'Classes' })).toBeVisible()

    await mountExternalLinkFixture(page)

    const before = page.url()
    await page.getByRole('button', { name: 'Keep focus here' }).focus()
    await page.getByRole('link', { name: 'Model markdown link' }).click()

    await expect(page).toHaveURL(before)
    await expect(page.getByRole('heading', { name: 'Classes' })).toBeVisible()
    await expect
      .poll(async () => await readOpenCalls(page))
      .toEqual([{ command: 'open_external_url', args: { url: 'https://example.com/docs' } }])
    expect(api.unexpectedRequests).toEqual([])
  })

  test('target blank links are intercepted instead of opening uncontrolled new windows', async ({
    page,
  }) => {
    const api = await installApiMocks(page)
    await installTauriInvokeMock(page)
    await page.goto('/#/')
    await page.waitForLoadState('networkidle')

    await mountExternalLinkFixture(page)

    const popup = page.waitForEvent('popup', { timeout: 1_000 }).catch(() => null)
    const before = page.url()
    await page.getByRole('link', { name: 'Target blank link' }).click()

    await expect(page).toHaveURL(before)
    await expect(await popup).toBeNull()
    await expect
      .poll(async () => await readOpenCalls(page))
      .toEqual([{ command: 'open_external_url', args: { url: 'https://example.com/paper' } }])
    expect(api.unexpectedRequests).toEqual([])
  })

  test('rejected unsafe links show an in-app error and keep both route and focus', async ({
    page,
  }) => {
    const api = await installApiMocks(page)
    await installTauriInvokeMock(page)
    await page.goto('/#/')
    await page.waitForLoadState('networkidle')

    await mountExternalLinkFixture(page)

    const before = page.url()
    await page.getByRole('button', { name: 'Keep focus here' }).focus()
    await page.getByRole('link', { name: 'Unsafe loopback link' }).click()

    await expect(page).toHaveURL(before)
    await expect(page.getByText('Lyra can only open public http or https links.')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Keep focus here' })).toBeFocused()
    await expect.poll(async () => await readOpenCalls(page)).toEqual([])
    expect(api.unexpectedRequests).toEqual([])
  })
})
