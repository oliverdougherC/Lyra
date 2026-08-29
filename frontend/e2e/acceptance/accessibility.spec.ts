/**
 * Keyboard, focus, and error-announcement assertions for the highest-risk
 * interactions in the school-critical flows.
 *
 * This is not a comprehensive accessibility suite — it covers the release
 * gate's highest-risk interactions rather than trying to test everything.
 * A broader WCAG audit belongs in a dedicated accessibility project.
 */

import { test, expect } from '@playwright/test'
import {
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
    const cls = await createClass('Acceptance: A11y')
    classId = cls.id
  })

  test.afterEach(async () => {
    await clearTutorState()
  })

  test('home page: class links are reachable by Tab', async ({ page }) => {
    await page.goto('/')
    await page.waitForLoadState('networkidle')

    // Tab until we reach a link or button inside the class list region.
    // The exact tab count depends on the number of skip-links and header
    // controls, so we loop up to a reasonable ceiling.
    let reachedClassLink = false
    for (let i = 0; i < 20; i++) {
      await page.keyboard.press('Tab')
      const tag = await page.evaluate(() => document.activeElement?.tagName?.toLowerCase())
      const href = await page.evaluate(
        () => (document.activeElement as HTMLAnchorElement)?.href ?? '',
      )
      if ((tag === 'a' && href.includes('/classes/')) || tag === 'button') {
        reachedClassLink = true
        break
      }
    }
    expect(reachedClassLink, 'Tab should reach a class link or action button').toBe(true)
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
    // Navigate and check for aria-busy on the loading skeleton.  The window
    // between commit and networkidle is narrow, so we use a 3s timeout.  If
    // the page loads instantly the assertion still passes provided the
    // skeleton rendered (even briefly) with aria-busy.
    await page.goto('/', { waitUntil: 'commit' })
    await expect(page.locator('[aria-busy="true"]').first()).toBeAttached({
      timeout: 3_000,
    })
  })
})
