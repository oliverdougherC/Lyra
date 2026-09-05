/**
 * PLA-316 regeneration in the real stack, on the class conversation (the agent's only surface).
 *
 * Proves the agent's regeneration end to end through the product UI: a successful
 * regeneration re-answers the last question and replaces the reply it already has, exactly
 * once - no duplicate assistant rows, no lost reply - and the replacement survives reload.
 *
 * The class conversation is agent-powered: one non-streaming turn that plans the work, runs
 * its tools, and returns the full reply. Regeneration takes the same shape - the server
 * re-runs the last turn (never re-plays a completed one) and supersedes the old reply the
 * moment the new one commits. The tutor fixture is pointed at the shared model endpoint, so
 * each agent model call returns the enqueued text and the loop stops (no tool calls), giving
 * a deterministic reply. We do not replace any of the backend's persistence with test doubles.
 */

import { resolve } from 'node:path'
import { test, expect, type Page } from '@playwright/test'
import {
  apiGet,
  createClass,
  createSession,
  sendChatMessage,
  waitForChatResponse,
  enqueueTutorResponse,
  clearTutorState,
  uploadDocument,
  waitForDocumentReady,
} from './helpers'

const TEST_DATA = resolve(__dirname, 'test-data')

const ANSWER_A = 'Deterministic original answer ALPHA for the regeneration test.'
const ANSWER_B = 'Deterministic replacement answer BETA after a successful regeneration.'

/** Open the class conversation on a specific session (the agent's surface). */
async function openChatSession(page: Page, classId: number, sessionId: number): Promise<void> {
  await page.goto(`/classes/${classId}/chat?session=${sessionId}`)
  await page.waitForLoadState('networkidle')
}

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
  const body = (await res.json()) as Array<{ role: string }>
  return body.filter((m) => m.role === 'assistant').length
}

/** Last assistant message content via the product API. */
async function lastAssistantContent(sessionId: number): Promise<string> {
  const res = await apiGet(`/api/sessions/${sessionId}/messages`)
  const body = (await res.json()) as Array<{ role: string; content: string }>
  const assistants = body.filter((m) => m.role === 'assistant')
  return assistants.length > 0 ? assistants[assistants.length - 1].content : ''
}

test.describe('PLA-316 regeneration (class conversation)', () => {
  let classId: number
  let sessionId: number

  test.beforeAll(async () => {
    const cls = await createClass('Regeneration Class')
    classId = cls.id
    sessionId = (await createSession(classId)).id
  })

  test.afterEach(async () => {
    await clearTutorState()
  })

  test('a successful regeneration replaces the reply exactly once and survives reload', async ({
    page,
  }) => {
    // 1. Create the conversation and obtain a complete original answer A.
    await enqueueTutorResponse(ANSWER_A)
    await openChatSession(page, classId, sessionId)
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

    // 2. Run a SUCCESSFUL regeneration through the product affordance. The agent re-answers
    //    the last question and supersedes the reply it already has; the fixture returns B.
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
    await expect(page.getByText('BETA').first()).toBeVisible({ timeout: 10_000 })
  })

  test('a manual regeneration to All material carries an explicit null scope (PLA-401 final pass)', async ({
    page,
  }) => {
    // The pin the review turns on: send under a selected document, switch the composer to
    // "All material", and Regenerate. The wire body must name the scope by property
    // presence - an EXPLICIT `document_id: null`, not an absent property - so the server
    // answers class-wide even though the stored scope is the document.
    const upload = await uploadDocument(
      classId,
      resolve(TEST_DATA, 'supplement.md'),
      'supplement.md',
    )
    // Document upload is accepted then processed asynchronously.
    expect(upload.status).toBe(202)
    const doc = (await upload.json()) as { id: number }
    await waitForDocumentReady(doc.id)

    // A fresh conversation of this class: the earlier test already left one answered
    // turn in the shared session, and this test counts replies in its own.
    const ownSession = await createSession(classId)

    await enqueueTutorResponse(ANSWER_A)
    await openChatSession(page, classId, ownSession.id)

    // Select the document in the source chip and send under it.
    const chip = page.locator('button[aria-label$="Choose what Lyra reads for this answer."]')
    await chip.click()
    await page.getByRole('radio', { name: 'supplement.md' }).click()
    await expect(chip).toContainText('supplement.md')
    await sendChatMessage(page, 'Explain the key idea in the supplement.')
    await waitForChatResponse(page)
    await expect(page.getByText('ALPHA').first()).toBeVisible({ timeout: 30_000 })

    // Capture the regenerate request and let it run against the real backend.
    const regenerateBodies: Array<Record<string, unknown>> = []
    await page.route('**/api/**/agent-chat/regenerate', async (route) => {
      const data = route.request().postDataJSON()
      regenerateBodies.push((data ?? {}) as Record<string, unknown>)
      await route.continue()
    })

    // Switch the composer to All material, then regenerate the reply.
    await chip.click()
    await page.getByRole('radio', { name: 'All material' }).click()
    await expect(chip).toContainText('All material')
    await enqueueTutorResponse(ANSWER_B)
    await clickRegenerateFor(page, 'ALPHA')

    await waitForChatResponse(page)
    await expect(regenerateBodies).toHaveLength(1)
    // Property presence on the wire: the key is present with an explicit null - an ABSENT
    // property (the pre-fix serialization) would have re-answered under the stored
    // document instead of class-wide.
    expect(regenerateBodies[0]).toHaveProperty('document_id', null)
    expect(regenerateBodies[0].mode).toBe('guide')
    // The regeneration still replaces exactly once, now class-wide.
    expect(await assistantCount(ownSession.id)).toBe(1)
    expect(await lastAssistantContent(ownSession.id)).toBe(ANSWER_B)
  })
})
