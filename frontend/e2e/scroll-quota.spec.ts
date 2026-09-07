import { expect, test, type Page } from '@playwright/test'

// These wrappers observe the browser's own History implementation. They deliberately
// rethrow native exceptions: no injected quota or mocked History behavior is used.
type Probe = { push: number; replace: number; scroll: number; errors: string[] }
type ProbedWindow = Window & { quotaProbe: Probe }
const filesSelector = '#documents-pane-body [data-slot="scroll-area-viewport"]'

async function prepare(page: Page) {
  await page.setViewportSize({ width: 768, height: 500 })
  await page.addInitScript(() => {
    const probe = { push: 0, replace: 0, scroll: 0, errors: [] as string[] }
    ;(window as unknown as ProbedWindow).quotaProbe = probe
    for (const method of ['pushState', 'replaceState'] as const) {
      const original = history[method].bind(history)
      history[method] = (...args: Parameters<History[typeof method]>) => {
        probe[method === 'pushState' ? 'push' : 'replace'] += 1
        try {
          return original(args[0], args[1], args[2])
        } catch (error) {
          probe.errors.push(`${method}: ${String(error)}`)
          throw error
        }
      }
    }
    document.addEventListener(
      'scroll',
      () => {
        probe.scroll += 1
      },
      true,
    )
  })
  const course = {
    id: 12,
    name: 'Synthetic laboratory course with a long title for return context',
    code: 'ECE 203',
    semester: 'Fall 2026',
    archived: false,
    document_count: 100,
    created_at: '2026-09-01T00:00:00Z',
    last_active_at: '2026-09-01T00:00:00Z',
  }
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname
    const data: Record<string, unknown> = {
      '/api/classes': Array.from({ length: 80 }, (_, i) => ({
        ...course,
        id: i ? i + 100 : 12,
        name: i ? `Synthetic class ${i}` : course.name,
      })),
      '/api/classes/12': course,
      '/api/classes/12/documents': Array.from({ length: 100 }, (_, i) => ({
        id: i + 1,
        class_id: 12,
        filename: `Laboratory-notes-near-identical-filename-appendix-${i}.pdf`,
        mime: 'application/pdf',
        byte_size: 1024,
        state: 'ready',
        pages_total: 2,
        pages_done: 2,
        pages_skipped: 0,
        pages_failed: 0,
        recognize: false,
        created_at: course.created_at,
      })),
      '/api/classes/12/sessions': [],
      '/api/classes/12/solutions': [],
      '/api/classes/12/drafts': [],
      '/api/classes/12/study': { decks: [], quizzes: [] },
      '/api/classes/12/profile': { facts: [], extraction_skipped_reason: null },
      '/api/settings': {
        endpoint_url: 'http://127.0.0.1:8080/v1',
        model: 'synthetic',
        endpoint_is_local: true,
        api_key_set: false,
        remote_ack: false,
        context_window: 8192,
      },
      '/api/desktop-import/status': { available: false, status: 'idle', destination_ready: false },
    }
    return route.fulfill({ json: data[path] ?? [] })
  })
  await page.goto('/#/classes/12?tab=files')
  await expect(
    page.locator(filesSelector).getByText(/appendix-0.pdf/, { exact: false }),
  ).toBeVisible()
  await page
    .locator('#main-content')
    .getByRole('searchbox', { name: 'Filter documents by name' })
    .fill('appendix')
}

async function positions(page: Page) {
  return page.evaluate(
    (selector) => ({
      main: document.querySelector('#main-content')!.scrollTop,
      files: document.querySelector(selector)!.scrollTop,
    }),
    filesSelector,
  )
}

async function immediateNavigate(page: Page) {
  // A real app link; click in this task without waiting for any quota window to expire.
  await page.evaluate(() => {
    const link = Array.from(document.querySelectorAll<HTMLAnchorElement>('a[href]')).find(
      (el) => el.getAttribute('href') === '/#/',
    )!
    link.click()
  })
}

for (const mode of ['unchanged nested burst', 'sustained tracked and nested scrolling'] as const) {
  test(`WebKit History headroom: ${mode} then immediate navigation`, async ({
    page,
    browser,
    browserName,
  }, testInfo) => {
    test.skip(
      browserName !== 'webkit',
      'Actual WebKit quota regression; Chromium has different limits.',
    )
    test.setTimeout(45_000)
    const pageErrors: string[] = []
    page.on('pageerror', (error) => pageErrors.push(error.message))
    await prepare(page)
    const start = await page.evaluate(() => ({ ...(window as unknown as ProbedWindow).quotaProbe }))
    const scrollRun = await page.evaluate(
      async ({ selector, mode }) => {
        const main = document.querySelector<HTMLElement>('#main-content')!
        const files = document.querySelector<HTMLElement>(selector)!
        // The file row is deliberately untracked. It models bubbling/capture events from
        // an outline or other nested content without changing either tracked position.
        const nested = files.querySelector<HTMLElement>('button')!
        const started = performance.now()
        let frames = 0
        if (mode === 'unchanged nested burst') {
          for (let i = 0; i < 650; i++) nested.dispatchEvent(new Event('scroll'))
        } else {
          do {
            await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
            frames += 1
            main.scrollTop = frames % 2 ? 64 : 128
            files.scrollTop = 250 + (frames % 80) * 4
            // Dispatch also exercises programmatic scroll notifications even when a
            // short main viewport clamps two successive positions to the same value.
            main.dispatchEvent(new Event('scroll'))
            files.dispatchEvent(new Event('scroll'))
            nested.dispatchEvent(new Event('scroll'))
          } while (performance.now() - started < 12_000 || frames < 600)
        }
        return {
          frames,
          elapsedMs: performance.now() - started,
          main: main.scrollTop,
          files: files.scrollTop,
          probe: { ...(window as unknown as ProbedWindow).quotaProbe },
        }
      },
      { selector: filesSelector, mode },
    )
    await immediateNavigate(page)
    const navigation = await page.evaluate(() => ({
      hash: location.hash,
      probe: { ...(window as unknown as ProbedWindow).quotaProbe },
    }))
    await testInfo.attach('native-history-measurements', {
      body: JSON.stringify(
        {
          browserVersion: browser.version(),
          userAgent: await page.evaluate(() => navigator.userAgent),
          mode,
          start,
          scrollRun,
          navigation,
          pageErrors,
        },
        null,
        2,
      ),
      contentType: 'application/json',
    })
    await page.screenshot({ path: testInfo.outputPath('immediate-navigation.png') })
    expect(navigation.hash).toBe('#/')
    expect(
      scrollRun.probe.replace - start.replace,
      'scroll must not spend native History quota',
    ).toBe(0)
    expect(scrollRun.probe.push - start.push).toBe(0)
    expect(navigation.probe.errors).toEqual([])
    expect(pageErrors).toEqual([])
    await expect(page.getByRole('heading', { name: 'Classes', exact: true })).toBeVisible()
    await page.goBack()
    await expect(
      page.locator('#main-content').getByRole('searchbox', { name: 'Filter documents by name' }),
    ).toHaveValue('appendix')
    await expect
      .poll(() => positions(page))
      .toEqual({ main: scrollRun.main, files: scrollRun.files })
    await page.goForward()
    await expect(page.getByRole('heading', { name: 'Classes', exact: true })).toBeVisible()
    await page.goBack()
    await expect
      .poll(() => positions(page))
      .toEqual({ main: scrollRun.main, files: scrollRun.files })
    await page.reload()
    await expect(
      page.locator('#main-content').getByRole('searchbox', { name: 'Filter documents by name' }),
    ).toHaveValue('appendix')
    await expect
      .poll(() => positions(page))
      .toEqual({ main: scrollRun.main, files: scrollRun.files })
  })
}

// Deliberate external quota consumption is kept separate from ordinary scrolling:
// these native exceptions are expected setup and must not disguise scroll regressions.
test('WebKit History headroom: externally exhausted native quota uses a same-document fallback', async ({
  page,
  browser,
  browserName,
}, testInfo) => {
  test.skip(browserName !== 'webkit', 'Actual WebKit quota and hash-navigation fallback.')
  test.setTimeout(30_000)
  const pageErrors: string[] = []
  let documentRequests = 0
  page.on('pageerror', (error) => pageErrors.push(error.message))
  page.on('request', (request) => {
    if (request.isNavigationRequest() && request.frame() === page.mainFrame()) documentRequests += 1
  })
  await prepare(page)
  const depletion = await page.evaluate(() => {
    const marker = crypto.randomUUID()
    ;(window as unknown as { quotaDocumentMarker: string }).quotaDocumentMarker = marker
    const started = performance.now()
    let successfulCalls = 0
    let nativeException: string | null = null
    for (let i = 0; i < 1000; i++) {
      try {
        history.replaceState(history.state, '')
        successfulCalls += 1
      } catch (error) {
        nativeException = String(error)
        break
      }
    }
    return {
      marker,
      successfulCalls,
      nativeException,
      elapsedMs: performance.now() - started,
      probe: { ...(window as unknown as ProbedWindow).quotaProbe },
    }
  })
  const requestsBeforeNavigation = documentRequests
  await immediateNavigate(page)
  const navigation = await page.evaluate(() => ({
    hash: location.hash,
    marker: (window as unknown as { quotaDocumentMarker: string }).quotaDocumentMarker,
    probe: { ...(window as unknown as ProbedWindow).quotaProbe },
  }))
  if (navigation.hash === '#/') {
    await expect(page.getByRole('heading', { name: 'Classes', exact: true })).toBeVisible()
  }
  const destinationRendered = await page
    .getByRole('heading', { name: 'Classes', exact: true })
    .isVisible()
  await testInfo.attach('native-quota-fallback-measurements', {
    body: JSON.stringify(
      {
        browserVersion: browser.version(),
        userAgent: await page.evaluate(() => navigator.userAgent),
        depletion,
        navigation,
        requestsBeforeNavigation,
        documentRequests,
        destinationRendered,
        pageErrors,
      },
      null,
      2,
    ),
    contentType: 'application/json',
  })
  await page.screenshot({ path: testInfo.outputPath('native-quota-fallback.png') })
  expect(depletion.nativeException).toMatch(/SecurityError/)
  expect(depletion.successfulCalls).toBeGreaterThan(0)
  expect(navigation.hash).toBe('#/')
  await expect(page.getByRole('heading', { name: 'Classes', exact: true })).toBeVisible()
  expect(navigation.marker, 'the original document must survive the fallback').toBe(
    depletion.marker,
  )
  expect(documentRequests, 'hash fallback must not request another document').toBe(
    requestsBeforeNavigation,
  )
  expect(navigation.probe.errors.slice(depletion.probe.errors.length)).toEqual(
    expect.arrayContaining([expect.stringMatching(/^pushState: SecurityError:/)]),
  )
  expect(pageErrors).toEqual([])
})
