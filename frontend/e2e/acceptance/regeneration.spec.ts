/**
 * PLA-316 cancelled regeneration in the real stack.
 *
 * Proves the merged regeneration implementation end to end through the product UI:
 * a cancellation mid-regeneration leaves the complete original reply durable and never
 * persists partial output, while a successful regeneration replaces the original exactly
 * once (no duplicate assistant rows) and survives reload.
 *
 * The tutor fixture is put in `partial-hold` mode so the regeneration's model call emits
 * a few real token frames and then holds the stream open -- the turn is genuinely in
 * flight with partial output begun when the student stops it. The production
 * `_commit_reply_atomic` path is what decides persistence: on cancel with a superseded
 * reply, nothing is committed and the original stays intact. We do not replace any of
 * that with test doubles.
 */

import { test, expect, type Page } from '@playwright/test'
import {
  apiGet,
  createClass,
  createSession,
  navigateToChat,
  sendChatMessage,
  waitForChatResponse,
  setTutorMode,
  enqueueTutorResponse,
  clearTutorState,
} from './helpers'

const ANSWER_A = 'Deterministic original answer ALPHA for the regeneration test.'
const ANSWER_B = 'Deterministic replacement answer BETA after a successful regeneration.'

/** Hover the reply containing `text` and click its regenerate affordance. */
async function clickRegenerateFor(page: Page, text: string): Promise<void> {
  const bubble = page.getByText(text).first()
  await bubble.hover()
  // The regenerate button sits in the same message group as the reply text.
  const group = bubble.locator('xpath=ancestor::div[contains(@class,"group")][1]')
  await group.getByRole('button', { name: 'Try this answer again' }).click()
}

/** Count of assistant messages in the conversation via the product API. */
async function assistantCount(sessionId: number): Promise<number> {
  const res = await apiGet(`/api/sessions/${sessionId}/messages`)
  const msgs = (await res.json()) as Array<{ role: string }>
  return msgs.filter((m) => m.role === 'assistant').length
}

/** Last assistant message content via the product API. */
async function lastAssistantContent(sessionId: number): Promise<string> {
  const res = await apiGet(`/api/sessions/${sessionId}/messages`)
  const msgs = (await res.json()) as Array<{ role: string; content: string }>
  const assistants = msgs.filter((m) => m.role === 'assistant')
  return assistants.length ? String(assistants[assistants.length - 1].content) : ''
}

test.describe('PLA-316 cancelled regeneration', () => {
  let classId: number
  let sessionId: number

  test.beforeAll(async () => {
    const cls = await createClass('Regeneration Class')
    classId = cls.id
    sessionId = (await createSession(classId)).id
  })

  test.afterEach(async () => {
    await setTutorMode('success')
    await clearTutorState()
  })

  test('cancelled regeneration keeps the original; a later success replaces it exactly once', async ({
    page,
  }) => {
    // 1. Create the conversation and obtain a complete original answer A.
    await enqueueTutorResponse(ANSWER_A)
    await navigateToChat(page, classId)
    await sendChatMessage(page, 'Give me the first law of thermodynamics.')
    await waitForChatResponse(page)
    // The reply is rendered and the turn has settled (the regenerate affordance mounts
    // only once the optimistic turn clears).
    await expect(page.getByText('ALPHA').first()).toBeVisible({ timeout: 30_000 })
    await page
      .locator('[aria-label="Try this answer again"]')
      .first()
      .waitFor({ state: 'attached', timeout: 15_000 })

    // Exactly one assistant reply now, and it is A.
    expect(await assistantCount(sessionId)).toBe(1)
    expect(await lastAssistantContent(sessionId)).toBe(ANSWER_A)

    // 2. Start regeneration through the product path. The fixture is in `partial-hold`
    //    mode: the model call emits partial token frames and then holds, so the turn is
    //    genuinely in flight with partial output begun when we stop it.
    await setTutorMode('partial-hold')
    await clickRegenerateFor(page, 'ALPHA')

    // Wait until the regeneration is actively streaming (the Stop control appears).
    const stopBtn = page.getByRole('button', { name: 'Stop generating' })
    await expect(stopBtn).toBeVisible({ timeout: 15_000 })

    // Give the fixture a moment to emit its partial frames so output has truly begun.
    await new Promise((r) => setTimeout(r, 400))

    // 3. Cancel/Stop the regeneration through the product interaction. This aborts the
    //    SSE fetch; the backend's stream generator is cancelled and, because this is a
    //    regeneration (a superseded reply), nothing partial is committed.
    await stopBtn.click()

    // The turn settles as stopped: the streaming Stop control disappears (the composer
    // returns to its idle Send state). This proves the cancellation landed and the turn
    // is no longer in flight.
    await expect(stopBtn).toBeHidden({ timeout: 15_000 })

    // 4. Reload/refetch the conversation through the product UI.
    await page.reload({ waitUntil: 'networkidle' })

    // Assert: the complete original A is still the durable assistant reply; the partial
    // regeneration output was not persisted; there is no duplicate assistant reply and no
    // missing-reply state.
    expect(await assistantCount(sessionId)).toBe(1)
    const contentAfterCancel = await lastAssistantContent(sessionId)
    expect(contentAfterCancel).toBe(ANSWER_A)
    await expect(page.getByText('ALPHA').first()).toBeVisible({ timeout: 10_000 })

    // 5. Now run a SUCCESSFUL regeneration and assert A is replaced exactly once by B,
    //    with no duplicate assistant rows, and that B survives reload.
    await setTutorMode('success')
    await enqueueTutorResponse(ANSWER_B)
    await clickRegenerateFor(page, 'ALPHA')

    // Wait for the successful regeneration to complete and commit B.
    await waitForChatResponse(page)
    await expect(page.getByText('BETA').first()).toBeVisible({ timeout: 30_000 })
    await page
      .locator('[aria-label="Try this answer again"]')
      .first()
      .waitFor({ state: 'attached', timeout: 15_000 })

    // A is replaced exactly once by B: still a single assistant reply, now B (no duplicate).
    expect(await assistantCount(sessionId)).toBe(1)
    expect(await lastAssistantContent(sessionId)).toBe(ANSWER_B)

    // B survives reload.
    await page.reload({ waitUntil: 'networkidle' })
    expect(await assistantCount(sessionId)).toBe(1)
    expect(await lastAssistantContent(sessionId)).toBe(ANSWER_B)
  })
})
