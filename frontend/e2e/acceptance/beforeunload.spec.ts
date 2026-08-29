/**
 * PLA-315 real-browser beforeunload proof.
 *
 * The merged unit/component implementation of `installBeforeUnloadGuard()` is already
 * accepted. This spec proves the browser contract end to end against the production
 * draft page: the guard is wired to the real save engine's dirty state, and the native
 * "unsaved changes" dialog appears exactly when there is unconfirmed work in flight and
 * disappears once that work is confirmed by the server.
 *
 * Mechanism (verified empirically against this Playwright/Chromium build): a page with
 * an armed beforeunload guard fires a `beforeunload` dialog on reload; `accept()`
 * proceeds with the unload, `dismiss()` cancels it. A clean page fires no dialog and
 * navigates immediately. We hold the ACTUAL autosave PATCH in flight (a route that does
 * not respond) so the production engine genuinely remains dirty/unconfirmed -- we do not
 * replace or stub the guard or the engine.
 */

import { test, expect } from '@playwright/test'
import { apiGet, createClass, createDraft } from './helpers'

test.describe('PLA-315 real-browser beforeunload', () => {
  test('hard unload is protected while a save is in flight, and clean once confirmed', async ({
    page,
  }) => {
    const cls = await createClass('BeforeUnload Class')
    const draft = await createDraft(cls.id, 'BeforeUnload Draft')

    // Phase 1: deterministically hold the real autosave PATCH so the engine stays
    // dirty/unconfirmed. The production onChange -> engine.schedule debounce arms an
    // autosave ~1.5s after typing; this route swallows it and never responds, so the
    // engine's write promise stays pending and isDirty() reports true.
    let heldPatchSeen = false
    await page.route(`**/api/drafts/${draft.id}/body`, async (route) => {
      if (!heldPatchSeen && route.request().method() === 'PATCH') {
        heldPatchSeen = true
        // Hold forever: do not fulfill, do not continue. The request is in flight.
        return
      }
      await route.continue()
    })

    await page.goto(`/classes/${cls.id}/drafts/${draft.id}`)
    const editor = page.locator('[aria-label="Draft document"]')
    await expect(editor).toBeVisible({ timeout: 15_000 })

    // Type unsaved text into the real editor and wait past the autosave debounce so the
    // (held) PATCH is genuinely in flight.
    await editor.click()
    await page.keyboard.type('Unsaved beforeunload probe text.', { delay: 15 })
    await page.waitForTimeout(2_500)
    expect(heldPatchSeen, 'autosave PATCH was not observed in flight').toBe(true)

    // Attempt a real hard reload. The production guard is armed (engine dirty), so the
    // browser must report the unload protection as a beforeunload dialog.
    let sawProtection = false
    const dialogPromise = page
      .waitForEvent('dialog', { timeout: 10_000 })
      .then(async (dialog) => {
        expect(dialog.type()).toBe('beforeunload')
        sawProtection = true
        await dialog.accept() // proceed with the unload
      })
      .catch(() => undefined)
    const reloadPromise = page.reload({ waitUntil: 'load', timeout: 15_000 }).catch(() => undefined)
    await Promise.allSettled([dialogPromise, reloadPromise])

    expect(
      sawProtection,
      'browser did not report unload protection while a save was in flight',
    ).toBe(true)

    // Phase 2: the reload cleared the editor and the held route. Let the real autosave
    // complete against the real server this time (no interception), then verify the
    // authoritative server body/version contains the edit.
    await page.unroute(`**/api/drafts/${draft.id}/body`)
    const editor2 = page.locator('[aria-label="Draft document"]')
    await expect(editor2).toBeVisible({ timeout: 15_000 })
    await editor2.click()
    await page.keyboard.type('Unsaved beforeunload probe text.', { delay: 15 })

    // Wait until the server authoritatively holds the edit (autosave landed).
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
    expect(bodyVersion > 0).toBe(true)

    // Give the engine a beat to settle to `saved` (its write promise resolves on the
    // server ack), then attempt the same unload again: there must be NO prompt.
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
