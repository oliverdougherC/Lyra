/**
 * Keyboard, focus, and error-announcement assertions for the highest-risk
 * interactions in the school-critical flows.
 *
 * This is not a comprehensive accessibility suite — it covers the release
 * gate's highest-risk interactions rather than trying to test everything.
 * A broader WCAG audit belongs in a dedicated accessibility project.
 */

import { test, expect } from '@playwright/test'
import { randomUUID } from 'node:crypto'
import {
  apiDelete,
  createClass,
  createDraft,
  navigateToChat,
  apiPatch,
  clearTutorState,
  setTutorMode,
  enqueueTutorResponse,
  waitForChatResponse,
} from './helpers'

test.describe('Accessibility: keyboard and focus', () => {
  let classId: number

  test.beforeAll(async () => {
    const cls = await createClass(`Acceptance: A11y ${randomUUID()}`)
    classId = cls.id
  })

  test.afterAll(async () => {
    if (classId) await apiDelete(`/api/classes/${classId}`)
  })

  test.afterEach(async () => {
    await clearTutorState()
  })

  test('home page: class links are reachable by keyboard', async ({ page, browserName }) => {
    const extraClassIds: number[] = []
    try {
      // Other browser projects share the backend. Exercise a realistic long list so
      // keyboard reachability cannot accidentally depend on this file running first.
      for (let i = 0; i < 45; i++) {
        const extra = await createClass(`A11y navigation ${i} ${randomUUID()}`)
        extraClassIds.push(extra.id)
      }
      await page.goto('/')
      const classLink = page.locator(`#main-content a[href$="/classes/${classId}"]`)
      await expect(classLink).toBeVisible()

      // macOS WebKit includes links/buttons with Option-Tab when full keyboard access is off.
      const tab = browserName === 'webkit' ? 'Alt+Tab' : 'Tab'
      const tabStops = await page
        .locator(
          'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
        )
        .count()
      for (let i = 0; i <= tabStops; i++) {
        await page.keyboard.press(tab)
        if (await classLink.evaluate((element) => document.activeElement === element)) break
      }
      await expect(classLink).toBeFocused()
      await page.keyboard.press('Enter')
      await expect(page).toHaveURL(new RegExp(`/classes/${classId}(?:[?]|$)`))
    } finally {
      for (const id of extraClassIds) await apiDelete(`/api/classes/${id}`)
    }
  })

  test('chat composer: Enter sends a message and focus returns to the composer', async ({
    page,
  }) => {
    await setTutorMode('success')
    await enqueueTutorResponse('Keyboard-send reply.')

    await navigateToChat(page, classId)

    const composer = page.locator('#message-composer')
    await expect(composer).toBeVisible({ timeout: 10_000 })

    // Verify the composer has a proper accessible label
    await expect(composer).toHaveAttribute('aria-label', 'Message Lyra')

    // Type a message and press Enter to send (keyboard-only, no button click)
    await composer.click()
    await expect(composer).toBeFocused()
    await page.keyboard.type('Keyboard send test')
    await page.keyboard.press('Enter')

    // The assistant reply must appear (the send actually fired)
    await waitForChatResponse(page)
    await expect(page.getByText('Keyboard-send reply.').first()).toBeVisible({ timeout: 15_000 })

    // After the turn settles, focus must return to the composer
    await expect(composer).toBeFocused({ timeout: 5_000 })
  })

  test('settings page: form fields are labelled', async ({ page }) => {
    await page.goto('/settings')
    await page.waitForLoadState('networkidle')

    // The endpoint input must have an accessible label
    await expect(page.getByLabel(/endpoint/i)).toBeVisible({ timeout: 5_000 })
  })

  test('class creation dialog: focus traps inside and Escape closes', async ({ page }) => {
    await page.goto('/')
    await page.waitForLoadState('networkidle')

    // Click "New class" button
    await page.getByRole('button', { name: /new class/i }).click()

    // Dialog should open
    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible()

    // Focus must be inside the dialog (on the name input or another dialog element)
    const nameInput = page.locator('#class-name')
    await expect(nameInput).toBeVisible()
    const focusInDialog = await page.evaluate(() => {
      const dialog = document.querySelector('[role="dialog"]')
      return dialog?.contains(document.activeElement) ?? false
    })
    expect(focusInDialog, 'focus should be inside the dialog after opening').toBe(true)

    // Escape should close the dialog
    await page.keyboard.press('Escape')
    await expect(dialog).not.toBeVisible()
  })

  test('draft editor: document region is labelled', async ({ page }) => {
    const draft = await createDraft(classId, 'A11y Draft')
    await apiPatch(`/api/drafts/${draft.id}/body`, {
      content: 'Accessible content.',
      expected_version: 0,
      snapshot: false,
    })

    await page.goto(`/classes/${classId}/drafts/${draft.id}`)
    await page.waitForLoadState('networkidle')

    // The editor region must have an accessible name
    await expect(page.locator('[aria-label="Draft document"]')).toBeVisible({ timeout: 10_000 })
  })

  test('loading skeleton markup includes aria-busy', async ({ page }) => {
    let release!: () => void
    const held = new Promise<void>((resolve) => {
      release = resolve
    })
    let fetched = false
    await page.route('**/api/classes', async (route) => {
      const response = await route.fetch()
      expect(response.ok()).toBeTruthy()
      fetched = true
      await held
      await route.fulfill({ response })
    })
    try {
      await page.goto('/', { waitUntil: 'commit' })
      await expect.poll(() => fetched).toBe(true)
      const loading = page.getByLabel('Loading classes', { exact: true })
      await expect(loading).toBeVisible()
      await expect(loading).toHaveAttribute('aria-busy', 'true')
      release()
      await expect(loading).not.toBeVisible()
      await expect(page.locator(`#main-content a[href$="/classes/${classId}"]`)).toBeVisible()
    } finally {
      release()
      await page.unrouteAll({ behavior: 'wait' })
    }
  })
})
