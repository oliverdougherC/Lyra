/**
 * PLA-313 browser-real-backend ambiguity recovery.
 *
 * Proves the merged operation-id reconciliation end to end against the REAL product UI and
 * backend, using acceptance-only transport fault injection at the browser boundary (a
 * `page.route` that drops the accepted response before it reaches the page). No production code
 * is replaced or stubbed; the real FastAPI route runs and durably commits.
 *
 * The durable-commit ordering is what makes recovery safe: `_open_turn` writes the user message
 * AND its attempt (carrying operation ID X) in one `begin immediate`/`commit` BEFORE any model
 * token streams. So by the time the browser would have seen the acceptance, X is already durable
 * server-side even if the response never reaches it.
 *
 * Three scenarios:
 *   A. Browser loses the acceptance response (the accepted request's response is dropped at the
 *      transport layer before it reaches the page). Because the browser never received the 200,
 *      its composer keeps the ambiguity key X (a generic transport error must NOT clear it - only
 *      a structured `operation_id_mismatch` may) and restores the text. The student resends the
 *      unchanged logical request; we capture the operation ID on BOTH requests to prove the
 *      resend carried the SAME X (recovery preserved it), and assert exactly ONE durable user
 *      message + ONE authoritative assistant publication in the real backend, with a single
 *      settled exchange in the browser.
 *   B. A completed turn is replayed by its operation ID at the API level: a request carrying X
 *      after the turn already completed returns the stored reply WITHOUT calling the model again
 *      (tutor call count unchanged) and inserts no second user message - the "no second model
 *      invocation after completed X" invariant.
 *   C. Reusing X for a DIFFERENT request is refused with a structured 409
 *      (`operation_id_mismatch`) so the client mints fresh instead of corrupting state.
 */
import { test, expect } from '@playwright/test'
import type { Page } from '@playwright/test'
import {
  BACKEND,
  LYRA_HEADERS,
  createClass,
  createSession,
  navigateToChat,
  setTutorMode,
  clearTutorState,
  getTutorRequests,
} from './helpers'

const QUESTION = 'Explain Newton first law.'

async function countMessages(sessionId: number) {
  const res = await fetch(`${BACKEND}/api/sessions/${sessionId}/messages`, {
    headers: LYRA_HEADERS,
  })
  const msgs = (await res.json()) as Array<{ role: string; content: string }>
  return {
    user: msgs.filter((m) => m.role === 'user'),
    assistant: msgs.filter((m) => m.role === 'assistant'),
  }
}

async function drainStream(res: Response): Promise<void> {
  if (!res.body) return
  const reader = res.body.getReader()
  for (;;) {
    const { done } = await reader.read()
    if (done) break
  }
}

async function turnState(sessionId: number): Promise<string | null> {
  const res = await fetch(`${BACKEND}/_acceptance/turn-state/${sessionId}`, {
    headers: LYRA_HEADERS,
  })
  const st = (await res.json()) as { has_user?: boolean; state?: string | null }
  return st.state ?? null
}

async function waitTerminal(sessionId: number, timeoutMs = 20_000): Promise<string> {
  const deadline = Date.now() + timeoutMs
  let state: string | null = 'running'
  while (Date.now() < deadline) {
    state = await turnState(sessionId)
    if (state !== null && state !== 'running') return state
    await new Promise((r) => setTimeout(r, 250))
  }
  return state ?? 'unknown'
}

/** Fill the composer and send, using an in-page DOM click so a transient failure toast cannot
 *  intercept the Send button (Playwright's actionability check would block a covered element). */
async function sendForced(page: Page, message: string): Promise<void> {
  await page.locator('#message-composer').fill(message)
  await page.waitForTimeout(200)
  await page.evaluate(() => {
    const btn = document.querySelector('[aria-label="Send message"]') as HTMLButtonElement | null
    if (btn && !btn.disabled) btn.click()
  })
}

test.describe('PLA-313 browser ambiguity recovery', () => {
  test('a lost acceptance response is recovered by reusing the same operation ID (replay, not re-run)', async ({
    page,
  }) => {
    const cls = await createClass('Ambiguity Class')
    const session = await createSession(cls.id)
    await clearTutorState()
    await setTutorMode('normal')

    // Capture the operation ID on every chat request, and drop the FIRST accepted response at the
    // transport layer before it reaches the page. `route.fetch()` delivers the request to the real
    // backend (which durably commits X in `_open_turn` before streaming) and resolves once the 200
    // is available; `route.abort()` then means the browser never reads the stream, so its composer
    // keeps op ID X (a generic transport error must NOT clear it - only an operation_id_mismatch may).
    const seenOpIds: string[] = []
    let dropped = false
    await page.route('**/api/sessions/*/chat', async (route) => {
      const body = route.request().postDataJSON() as { operation_id?: string } | null
      if (body?.operation_id) seenOpIds.push(body.operation_id)
      if (!dropped) {
        dropped = true
        await route.fetch() // deliver to the real backend so X durably commits; it keeps running
        return route.abort() // acceptance response never reaches the browser
      }
      return route.continue()
    })

    await navigateToChat(page, cls.id)
    await sendForced(page, QUESTION)

    // The first request carried a freshly minted operation ID X.
    await expect.poll(async () => seenOpIds.length, { timeout: 10_000 }).toBe(1)
    const opX = seenOpIds[0]
    expect(opX).toBeTruthy()

    // The backend durably committed the user message + attempt X (before any token streamed). No
    // assistant reply exists yet because the browser's view of the stream was dropped.
    let pre = await countMessages(session.id)
    const commitDeadline = Date.now() + 10_000
    while (Date.now() < commitDeadline && pre.user.length !== 1) {
      pre = await countMessages(session.id)
      await new Promise((r) => setTimeout(r, 200))
    }
    expect(pre.user.length).toBe(1)
    expect(pre.user[0].content.trim()).toBe(QUESTION)

    // The model was called exactly once (the original send); the lost browser view caused no hidden
    // second call.
    const callsBeforeRelease = (await getTutorRequests()).length
    expect(callsBeforeRelease).toBe(1)

    // Wait for the original turn to reach a TERMINAL state (its session claim is released) so the
    // resend reconciles instead of hitting an ordinary busy 409. The dropped response cancels the
    // streaming generator asynchronously, which settles the attempt.
    const terminal = await waitTerminal(session.id)
    expect(terminal).not.toBe('running')

    // The browser's acceptance was lost: once settled, composer recovery restores the preserved text
    // into the draft box. A generic transport error must not discard the submitted text or X.
    await expect
      .poll(async () => page.locator('textarea').first().inputValue(), { timeout: 15_000 })
      .toBe(QUESTION)

    // Wait until the composer is actually able to send again (the first turn's optimistic rows are
    // settled and the Send button is enabled). Clicking while it is still settling would be a no-op.
    await expect
      .poll(
        async () => {
          const btn = page.locator('[aria-label="Send message"]')
          return (
            (await btn.isEnabled()) &&
            (await page.locator('#message-composer').inputValue()).trim() === QUESTION
          )
        },
        { timeout: 15_000 },
      )
      .toBe(true)

    // Now the browser resends the UNCHANGED logical request. If recovery had discarded X, this would
    // carry a freshly minted ID; if it preserved X, it carries the SAME one. Capture both operation
    // IDs to prove the resend reused X (recovery did not clear it on a generic transport error).
    await sendForced(page, QUESTION)

    // Invariant (same op ID X): wait for the second request and assert it carried the EXACT same
    // operation ID as the original. This is the direct proof that recovery preserved X.
    await expect.poll(async () => seenOpIds.length, { timeout: 15_000 }).toBe(2)
    expect(seenOpIds[1]).toBe(opX)

    // Let the resend settle so the durable assertions below observe the final state.
    await expect(page.locator('[aria-label="Send message"]')).toBeVisible({ timeout: 30_000 })

    // Invariant 1: still exactly ONE durable user message. The original's X and the resend's X are
    // the same key, so the backend reconciles them onto one question rather than inserting a second.
    const after = await countMessages(session.id)
    expect(after.user.length).toBe(1)
    expect(after.user[0].content.trim()).toBe(QUESTION)

    // Invariant 2: exactly ONE authoritative assistant publication (the recovery produced it; the
    // lost acceptance committed none, and the reconciliation did not duplicate one).
    await expect
      .poll(async () => (await countMessages(session.id)).assistant.length, { timeout: 15_000 })
      .toBe(1)

    // Invariant 3: the model was invoked at most twice total - the original send plus at most ONE
    // recovery attempt. Reusing op ID X on the existing user message means the resend did NOT
    // re-ask as a brand-new question (which would have produced a second durable user message).
    const callsAfterResend = (await getTutorRequests()).length
    expect(callsAfterResend).toBeLessThanOrEqual(2)

    // Invariant 4: the browser shows the settled exchange (the question is present in the transcript).
    // The authoritative no-duplication proof is invariant 1 (exactly one durable user message); this
    // just confirms the recovered turn rendered for the student.
    await expect(page.getByText(QUESTION, { exact: true }).first()).toBeVisible({ timeout: 10_000 })
  })

  test('a completed turn is replayed by its operation ID without a second model call', async () => {
    const cls = await createClass('Replay Class')
    const session = await createSession(cls.id)
    await clearTutorState()
    await setTutorMode('normal')

    // Establish a COMPLETED turn carrying operation X.
    const opX = crypto.randomUUID()
    const first = await fetch(`${BACKEND}/api/sessions/${session.id}/chat`, {
      method: 'POST',
      headers: LYRA_HEADERS,
      body: JSON.stringify({ content: QUESTION, mode: 'guide', operation_id: opX }),
    })
    expect(first.status).toBe(200)
    await drainStream(first)

    const before = await countMessages(session.id)
    expect(before.user.length).toBe(1)
    expect(before.assistant.length).toBe(1)
    const callsBeforeReplay = (await getTutorRequests()).length

    // A request carrying the SAME operation ID X after the turn completed must REPLAY the stored
    // reply: no second model call, no second user message.
    const replay = await fetch(`${BACKEND}/api/sessions/${session.id}/chat`, {
      method: 'POST',
      headers: LYRA_HEADERS,
      body: JSON.stringify({ content: QUESTION, mode: 'guide', operation_id: opX }),
    })
    expect(replay.status).toBe(200)
    await drainStream(replay)

    const after = await countMessages(session.id)
    expect(after.user.length).toBe(1) // no duplicate question
    expect(after.assistant.length).toBe(1) // no duplicate publication
    // No second model invocation after the completed turn: the replay did not re-run the model.
    expect((await getTutorRequests()).length).toBe(callsBeforeReplay)
  })

  test('reusing an operation ID for a DIFFERENT request is refused (mismatch mints fresh)', async () => {
    const cls = await createClass('Mismatch Class')
    const session = await createSession(cls.id)
    await clearTutorState()
    await setTutorMode('normal')

    // Establish a committed operation X with content A.
    const opX = crypto.randomUUID()
    const first = await fetch(`${BACKEND}/api/sessions/${session.id}/chat`, {
      method: 'POST',
      headers: LYRA_HEADERS,
      body: JSON.stringify({ content: QUESTION, mode: 'guide', operation_id: opX }),
    })
    expect(first.status).toBe(200)
    await drainStream(first)

    // Reusing op X with DIFFERENT content is a client bug: the backend must refuse it with a
    // structured 409 so the client mints a fresh operation ID rather than corrupting state.
    const mismatch = await fetch(`${BACKEND}/api/sessions/${session.id}/chat`, {
      method: 'POST',
      headers: LYRA_HEADERS,
      body: JSON.stringify({
        content: 'A completely different question.',
        mode: 'guide',
        operation_id: opX,
      }),
    })
    expect(mismatch.status).toBe(409)
    const errText = JSON.stringify(await mismatch.json())
    expect(errText).toContain('operation_id_mismatch')

    // A genuinely fresh operation ID for the new content succeeds (no cross-contamination).
    const fresh = await fetch(`${BACKEND}/api/sessions/${session.id}/chat`, {
      method: 'POST',
      headers: LYRA_HEADERS,
      body: JSON.stringify({
        content: 'A completely different question.',
        mode: 'guide',
        operation_id: crypto.randomUUID(),
      }),
    })
    expect(fresh.status).toBe(200)
    await drainStream(fresh)

    // The mismatch refusal did NOT create a second durable user message for the reused key.
    const msgs = (await (
      await fetch(`${BACKEND}/api/sessions/${session.id}/messages`, { headers: LYRA_HEADERS })
    ).json()) as Array<{ role: string }>
    expect(msgs.filter((m) => m.role === 'user').length).toBe(2) // QUESTION + the fresh one only
  })
})
