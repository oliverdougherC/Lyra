import { mkdir } from 'node:fs/promises'
import { expect, test, type Page } from '@playwright/test'

import { CLASS_ID, SESSION_ID, TWIN_SESSION_ID, installLyraApi } from './pla-504-math-fixture'

const activityPath = `/api/classes/${CLASS_ID}/sessions/${SESSION_ID}/agent/activity`
const recoveredEvents = [
  {
    id: 'bad-arguments',
    tool: 'read_file',
    capability: 'workspace_read',
    effect: 'read',
    state: 'failed',
    target_kind: 'file',
    target_id: 'missing.py',
    error_message: 'Invalid path',
    started_at: '2026-08-20T10:01:00Z',
    finished_at: '2026-08-20T10:01:01Z',
    result_summary: null,
  },
  {
    id: 'correct-arguments',
    tool: 'read_file',
    capability: 'workspace_read',
    effect: 'read',
    state: 'succeeded',
    target_kind: 'file',
    target_id: 'main.py',
    error_message: null,
    started_at: '2026-08-20T10:01:02Z',
    finished_at: '2026-08-20T10:01:03Z',
    result_summary: null,
  },
]

async function geometry(page: Page) {
  return page.evaluate(() => {
    const input = document.querySelector<HTMLTextAreaElement>(
      'textarea[aria-label="Message Lyra"]',
    )!
    const viewport = document.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]')!
    const transcript = viewport.firstElementChild!.getBoundingClientRect()
    const composer = input.getBoundingClientRect()
    const reader = viewport.getBoundingClientRect()
    return {
      transcriptTop: transcript.top,
      composerTop: composer.top,
      viewportTop: reader.top,
      viewportBottom: reader.bottom,
      scrollTop: viewport.scrollTop,
    }
  })
}

for (const variant of [
  { name: 'wide', width: 900, height: 650, zoom: 1 },
  { name: 'narrow-large-text', width: 390, height: 560, zoom: 1.25 },
])
  test(`history availability leaves transcript and composer geometry unchanged: ${variant.name}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: variant.width, height: variant.height })
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await installLyraApi(page, {
      [`/api/classes/${CLASS_ID}/sessions/${SESSION_ID}/agent/access-dismissals`]: {
        dismissals: [],
      },
    })
    let release!: () => void
    const pending = new Promise<void>((resolve) => {
      release = resolve
    })
    await page.route(`**${activityPath}`, async (route) => {
      await pending
      await route.fulfill({ json: recoveredEvents })
    })
    await page.goto(`/#/classes/${CLASS_ID}/chat?session=${SESSION_ID}`)
    await expect(page.getByRole('textbox', { name: 'Message Lyra' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Activity history' })).toHaveCount(0)
    await page.evaluate((zoom) => {
      document.documentElement.style.zoom = String(zoom)
    }, variant.zoom)
    await page.evaluate(async () => {
      await document.fonts.ready
      await new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      )
      const viewport = document.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]')!
      viewport.scrollTop = 0
    })
    const before = await geometry(page)
    await mkdir('test-results', { recursive: true })
    await page.screenshot({ path: `test-results/activity-before-${variant.name}.png` })

    release()
    await expect(page.getByRole('button', { name: 'Activity history' })).toBeVisible()
    const after = await geometry(page)
    await page.screenshot({ path: `test-results/activity-after-${variant.name}.png` })
    for (const key of [
      'transcriptTop',
      'composerTop',
      'viewportTop',
      'viewportBottom',
      'scrollTop',
    ] as const) {
      expect(
        Math.abs(after[key] - before[key]),
        `${key} changed on history availability`,
      ).toBeLessThanOrEqual(0.5)
    }
    await expect(page.getByText(/needs attention/)).toHaveCount(0)
    await page.getByRole('button', { name: 'Activity history' }).click()
    await expect(page.getByText('Invalid path')).toBeVisible()
    await page.reload()
    await expect(page.getByRole('button', { name: 'Activity history' })).toBeVisible()
    await expect(page.getByText(/needs attention/)).toHaveCount(0)
    await page.goto(`/#/classes/${CLASS_ID}/chat?session=${TWIN_SESSION_ID}`)
    await expect(page.getByRole('textbox', { name: 'Message Lyra' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Activity history' })).toHaveCount(0)
    await page.goto(`/#/classes/${CLASS_ID}/chat?session=${SESSION_ID}`)
    await expect(page.getByRole('button', { name: 'Activity history' })).toBeVisible()
    await expect(page.getByText(/needs attention/)).toHaveCount(0)
  })
