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

import { test, expect, type Page } from '@playwright/test'
import {
  apiGet,
  createClass,
  createSession,
  sendChatMessage,
  waitForChatResponse,
  enqueueTutorResponse,
  clearTutorState,
} from './helpers'

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
})
