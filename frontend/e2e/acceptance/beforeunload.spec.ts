/**
 * PLA-315 real-browser beforeunload proof.
 *
 * Proves the complete durability sequence: the guard prevents loss of an in-flight
 * edit, and releasing the held save makes it authoritative on the server.
 *
 * Required causal chain:
 *   type edit E
 *   -> actual production autosave PATCH for E begins
 *   -> deterministically hold THAT request via a Playwright route barrier
 *   -> production save engine is dirty/unconfirmed
 *   -> attempt hard reload/navigation
 *   -> native beforeunload fires
 *   -> DISMISS/CANCEL the unload
 *   -> page remains alive with E in the editor
 *   -> release the SAME held PATCH via the barrier
 *   -> real backend confirms it
 *   -> authoritative server body/version contains E
 *   -> save engine settles clean
 *   -> attempt hard unload again
 *   -> NO beforeunload prompt (engine is clean)
 *
 * E is never retyped after the first input. The guard protects the SAME edit throughout.
 */

import { test, expect, type Route } from '@playwright/test'
import { apiGet, createClass, createDraft } from './helpers'

test.describe('PLA-315 real-browser beforeunload', () => {
  test('hard unload is protected while a save is in flight, and clean once confirmed', async ({
    page,
  }) => {
    const cls = await createClass('BeforeUnload Class')
    const draft = await createDraft(cls.id, 'BeforeUnload Draft')

    // Phase 1: set up a deterministic route barrier that holds the FIRST autosave
    // PATCH and releases it only when we call releasePatch(). The route handler
    // awaits a promise that we resolve externally, so the PATCH is genuinely in
    // flight (the browser's fetch is pending, the engine's write promise is
    // unresolved, isDirty() reports true).
    let patchArrived: () => void
    const patchArrivedPromise = new Promise<void>((r) => {
      patchArrived = r
    })
    let releasePatch: () => void
    const patchReleasePromise = new Promise<void>((r) => {
      releasePatch = r
    })
    let firstPatchHeld = false

    await page.route(`**/api/drafts/${draft.id}/body`, async (route: Route) => {
      if (route.request().method() === 'PATCH' && !firstPatchHeld) {
        firstPatchHeld = true
        patchArrived!()
        await patchReleasePromise
        await route.continue()
        return
      }
      await route.continue()
    })

    await page.goto(`/classes/${cls.id}/drafts/${draft.id}`)
    const editor = page.locator('[aria-label="Draft document"]')
    await expect(editor).toBeVisible({ timeout: 15_000 })

    // Type unsaved text E into the real editor.
    await editor.click()
    await page.keyboard.type('Unsaved beforeunload probe text.', { delay: 15 })

    // Wait for the autosave debounce to fire and the (held) PATCH to arrive.
    await patchArrivedPromise
    expect(firstPatchHeld, 'autosave PATCH was not observed in flight').toBe(true)

    // Phase 2: attempt a real hard reload. The production guard is armed (engine
    // dirty with an unconfirmed write), so the browser fires a beforeunload dialog.
    // We DISMISS it (cancel the unload) so the page stays alive with E.
    let sawProtection = false
    page.once('dialog', async (dialog) => {
      expect(dialog.type()).toBe('beforeunload')
      sawProtection = true
      await dialog.dismiss()
    })

    // page.reload() with a dismissed beforeunload cancels the navigation.
    await page.reload({ timeout: 5_000 }).catch(() => {
      /* navigation cancelled by dismissed dialog */
    })

    expect(
      sawProtection,
      'browser did not report unload protection while a save was in flight',
    ).toBe(true)

    // Phase 3: the page is still alive. The editor must still contain E -- the
    // dismissed dialog preserved it. E is NOT retyped.
    await expect(editor).toContainText('Unsaved beforeunload probe text.', { timeout: 5_000 })

    // Phase 4: release the held PATCH so it completes to the real server.
    releasePatch!()

    // Wait until the server authoritatively holds E (the real PATCH landed).
    const deadline = Date.now() + 15_000
    let serverBody = ''
    let bodyVersion = 0
    while (Date.now() < deadline) {
      const d = await (await apiGet(`/api/drafts/${draft.id}`)).json()
      serverBody = String(d.body)
      bodyVersion = Number(d.body_version ?? 0)
      if (serverBody.includes('Unsaved beforeunload probe text.')) break
      await new Promise((r) => setTimeout(r, 300))
    }
    expect(serverBody).toContain('Unsaved beforeunload probe text.')
    expect(bodyVersion).toBeGreaterThan(0)

    // Phase 5: the save engine should now settle to clean (the write promise
    // resolved on the server ack). A second hard unload must produce NO prompt.
    await page.waitForTimeout(500)
    let sawSecondProtection = false
    page.once('dialog', async (dialog) => {
      if (dialog.type() === 'beforeunload') {
        sawSecondProtection = true
        await dialog.accept()
      }
    })
    await page.reload({ waitUntil: 'load', timeout: 15_000 })
    expect(
      sawSecondProtection,
      'unexpected unsaved-changes prompt after the save was confirmed',
    ).toBe(false)
  })
})
