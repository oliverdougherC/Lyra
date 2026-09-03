/**
 * Ambiguity recovery in the final single-conversation product.
 *
 * The ordinary class conversation speaks to the contextual agent: a turn POSTs to the
 * non-streaming agent endpoint and returns exactly one reply. When the browser loses the
 * acceptance (the response never reaches the page), the durable truth is server-side: the
 * attempt and its reply are already committed. The conversation recovers in the ordinary
 * transcript - the turn's refresh re-fetches the messages and the committed reply is what
 * the student sees - and a retry of the turn REPLAYS the stored reply instead of re-running
 * the model (PLA-295). Proven end to end against the real product UI and backend with
 * acceptance-only transport fault injection at the browser boundary (a `page.route` that
 * drops the accepted response before it reaches the page). No production code is replaced
 * or stubbed; the real FastAPI route runs and durably commits.
 *
 * The PLA-313 operation-ID idempotency contract is honored by the session chat endpoint
 * (the streaming tutor path), which remains available to API clients; its guarantees are
 * covered at the API level below: replay of a completed turn, structured mismatch refusal,
 * and - for the agent path - busy-409 serialisation plus replay after settlement.
 */
import { test, expect } from '@playwright/test'
import type { Page } from '@playwright/test'
import {
  BACKEND,
  LYRA_HEADERS,
  apiGet,
  apiPost,
  createClass,
  createSession,
  navigateToChat,
  setTutorMode,
  clearTutorState,
  getTutorRequests,
  waitForBarrier,
  releaseBarrier,
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

test.describe('PLA-313/PLA-295 conversation ambiguity recovery', () => {
  test.afterEach(async () => {
    // Leave the fixture in a clean state: a leftover barrier/error mode would poison the
    // next spec's turns (the fixture is shared across the whole acceptance run).
    await clearTutorState()
  })

  test('a lost agent acceptance is recovered in the ordinary transcript (replay, not re-run)', async ({
    page,
  }) => {
    const cls = await createClass('Ambiguity Class')
    const session = await createSession(cls.id)
    await clearTutorState()
    await setTutorMode('success')

    // Drop the FIRST agent turn's acceptance at the browser boundary. The agent endpoint is
    // non-streaming: route.fetch() returns only after the backend has fully committed the
    // turn and produced its JSON response, so aborting afterwards pins the scenario to the
    // one the test names - the turn happened, its acceptance was lost. The browser never
    // reads a byte of the response.
    let dropped = false
    await page.route('**/api/classes/*/sessions/*/agent-chat', async (route) => {
      if (!dropped) {
        dropped = true
        await route.fetch()
        return route.abort()
      }
      return route.continue()
    })

    await navigateToChat(page, cls.id)
    await sendForced(page, QUESTION)

    // The model was called exactly once (the original send). POLL to one: the durable commit
    // arrives, so a one-shot count can race it.
    await expect.poll(async () => (await getTutorRequests()).length, { timeout: 15_000 }).toBe(1)

    // The backend durably committed the user message AND the reply.
    let committed: { user: Array<{ content: string }>; assistant: Array<{ content: string }> } = {
      user: [],
      assistant: [],
    }
    await expect
      .poll(
        async () => {
          committed = await countMessages(session.id)
          return committed.user.length === 1 && committed.assistant.length === 1
        },
        { timeout: 15_000 },
      )
      .toBe(true)
    expect(committed.user[0].content.trim()).toBe(QUESTION)
    const committedReply = committed.assistant[0].content

    // The browser's acceptance was lost, but the conversation self-heals in the ordinary
    // transcript: the failed turn refreshes the messages, and the committed reply is what the
    // student sees. No resend is required to see the answer.
    await expect(page.getByText(QUESTION, { exact: true }).first()).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText(/deterministic response/i).first()).toBeVisible({ timeout: 15_000 })

    // A retry of the already-completed turn (the lost-response case, PLA-295) REPLAYS the
    // stored reply: no second model call, no second user message, no second publication.
    const retryRes = await fetch(
      `${BACKEND}/api/classes/${cls.id}/sessions/${session.id}/agent-chat/retry`,
      { method: 'POST', headers: LYRA_HEADERS },
    )
    expect(retryRes.status).toBe(200)
    const replay = (await retryRes.json()) as { message_id: number; content: string }
    expect(replay.content).toBe(committedReply)

    const after = await countMessages(session.id)
    expect(after.user.length).toBe(1)
    expect(after.assistant.length).toBe(1)
    expect(after.assistant[0].content).toBe(committedReply)
    expect((await getTutorRequests()).length).toBe(1)
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

  test('a concurrent agent turn is refused with 409, then replays after settlement', async () => {
    const cls = await createClass('Busy Agent Class')
    const session = await createSession(cls.id)
    await clearTutorState()
    await setTutorMode('barrier')

    // The first turn's model call is held at the fixture's barrier, keeping the session claim.
    const first = fetch(`${BACKEND}/api/classes/${cls.id}/sessions/${session.id}/agent-chat`, {
      method: 'POST',
      headers: LYRA_HEADERS,
      body: JSON.stringify({ content: QUESTION }),
    })
    await waitForBarrier()

    // A second turn on the same session while the claim is held is refused with a busy 409:
    // at most one agent turn runs at a time.
    const busy = await fetch(`${BACKEND}/api/classes/${cls.id}/sessions/${session.id}/agent-chat`, {
      method: 'POST',
      headers: LYRA_HEADERS,
      body: JSON.stringify({ content: QUESTION }),
    })
    expect(busy.status).toBe(409)

    // Release the barrier: the original turn completes and commits its reply.
    await releaseBarrier('Newton first law: an object in motion stays in motion.')
    const firstRes = await first
    expect(firstRes.status).toBe(200)
    const firstBody = (await firstRes.json()) as { message_id: number; content: string }

    const callsBeforeRetry = (await getTutorRequests()).length

    // A retry of the already-completed turn (the lost-acceptance case, PLA-295) REPLAYS the
    // stored reply: no second model call, no second user message, no second publication.
    const retryRes = await fetch(
      `${BACKEND}/api/classes/${cls.id}/sessions/${session.id}/agent-chat/retry`,
      { method: 'POST', headers: LYRA_HEADERS },
    )
    expect(retryRes.status).toBe(200)
    const replayBody = (await retryRes.json()) as { message_id: number; content: string }
    expect(replayBody.message_id).toBe(firstBody.message_id)
    expect(replayBody.content).toBe(firstBody.content)
    expect((await getTutorRequests()).length).toBe(callsBeforeRetry)

    const msgs = await countMessages(session.id)
    expect(msgs.user.length).toBe(1)
    expect(msgs.assistant.length).toBe(1)
  })

  test('a lost acceptance on a failing agent turn leaves a truthful failed attempt', async () => {
    const cls = await createClass('Lost Failure Class')
    const session = await createSession(cls.id)
    await clearTutorState()
    await setTutorMode('error-before-stream')

    // The turn fails at the model call (injected 500 upstream); the endpoint reports a 502 to
    // the client and persists a failed, retryable agent attempt.

    const res = await fetch(`${BACKEND}/api/classes/${cls.id}/sessions/${session.id}/agent-chat`, {
      method: 'POST',
      headers: LYRA_HEADERS,
      body: JSON.stringify({ content: QUESTION }),
    })
    expect(res.status).toBe(502)

    // The expected injection is consumed from the backend failure ledger so the run's
    // accounting gate only sees genuinely unexpected failures.
    const consumed = await (
      await apiPost('/_acceptance/backend-failures/consume', {
        method: 'POST',
        route: '/api/classes/{class_id}/sessions/{session_id}/agent-chat',
      })
    ).json()
    const ledger = (await apiGet('/_acceptance/backend-failures')).json()
    expect(
      consumed,
      `unconsumed ledger: ${JSON.stringify((await ledger).unconsumed)}`,
    ).toMatchObject({
      ok: true,
    })

    // The failed attempt is durable: exactly one user message, no assistant publication,
    // and the attempt state is 'failed' (truthful and retryable).
    const msgs = (await (
      await fetch(`${BACKEND}/api/sessions/${session.id}/messages`, { headers: LYRA_HEADERS })
    ).json()) as Array<{ role: string; agent_attempt?: { state: string } | null }>
    const users = msgs.filter((m) => m.role === 'user')
    expect(users.length).toBe(1)
    expect(users[0].agent_attempt?.state).toBe('failed')
    expect(msgs.filter((m) => m.role === 'assistant').length).toBe(0)
  })

  test('the browser mints one operation ID per agent send and a fresh one per new message', async ({
    page,
  }) => {
    // PLA-313 in the real product: the ordinary composer carries a browser-minted
    // operation ID on every agent turn. One logical Send keeps its ID across ambiguous
    // resubmits (the unit tests cover the retention), and a new message mints a new one.
    const cls = await createClass('Operation Class')
    await createSession(cls.id)
    await clearTutorState()
    await setTutorMode('normal')

    const operationIds: Array<string | null> = []
    await page.route('**/api/classes/*/sessions/*/agent-chat', async (route) => {
      if (route.request().method() === 'POST') {
        const body = route.request().postDataJSON() as { operation_id?: string } | null
        operationIds.push(body?.operation_id ?? null)
      }
      return route.continue()
    })

    await navigateToChat(page, cls.id)
    await sendForced(page, 'First question for the operation ledger.')
    await expect.poll(async () => operationIds.length, { timeout: 15_000 }).toBe(1)
    expect(operationIds[0]).toEqual(expect.any(String))
    expect(typeof operationIds[0]).toBe('string')
    expect((operationIds[0] as string).length).toBeGreaterThan(0)

    await page.waitForTimeout(1500)
    await sendForced(page, 'A different question, a different key.')
    await expect.poll(async () => operationIds.length, { timeout: 15_000 }).toBe(2)
    expect(operationIds[1]).toEqual(expect.any(String))
    expect(operationIds[1]).not.toBe(operationIds[0])
  })

  test('Stop cancels the in-flight agent turn on the real stack and the session stays usable', async ({
    page,
  }) => {
    // The non-streaming agent handler cannot see its client's disconnect, so the product's
    // Stop is explicit: it posts /agent-chat/stop, the backend cancels the in-flight task,
    // settles the durable attempt as stopped, and releases the session claim. Proven with the
    // turn held at the tutor fixture's barrier and the real Stop button in the product UI.
    const cls = await createClass('Stop Class')
    const session = await createSession(cls.id)
    await clearTutorState()
    await setTutorMode('barrier')

    let stopHits = 0
    await page.route('**/api/classes/*/sessions/*/agent-chat/stop', async (route) => {
      stopHits += 1
      return route.continue()
    })

    await navigateToChat(page, cls.id)
    await sendForced(page, 'A turn that will be stopped.')
    // The turn's model call is held at the barrier: the session claim is live.
    await waitForBarrier()

    // The real Stop button, not a backend call: the UI posts the explicit stop.
    await page.getByLabel('Stop generating').click()
    await expect.poll(async () => stopHits, { timeout: 15_000 }).toBe(1)

    // The durable attempt settled as stopped (not failed, not running), and no reply
    // was published by the half-run turn.
    let stopped = false
    await expect
      .poll(
        async () => {
          const msgs = (await (
            await fetch(`${BACKEND}/api/sessions/${session.id}/messages`, {
              headers: LYRA_HEADERS,
            })
          ).json()) as Array<{ role: string; agent_attempt?: { state: string } | null }>
          stopped =
            msgs.some((m) => m.role === 'user' && m.agent_attempt?.state === 'stopped') &&
            msgs.filter((m) => m.role === 'assistant').length === 0
          return stopped
        },
        { timeout: 15_000 },
      )
      .toBe(true)

    // Let the held fixture request settle (it was cancelled mid-flight), switch the fixture
    // back to normal responses, and the claim is free: the very next turn in the same
    // conversation runs to completion.
    await releaseBarrier('It stopped, but the next one lands.')
    await setTutorMode('normal')
    await sendForced(page, 'Can you continue now?')
    await expect
      .poll(
        async () => {
          const msgs = (await (
            await fetch(`${BACKEND}/api/sessions/${session.id}/messages`, {
              headers: LYRA_HEADERS,
            })
          ).json()) as Array<{ role: string }>
          return msgs.filter((m) => m.role === 'assistant').length
        },
        { timeout: 20_000 },
      )
      .toBeGreaterThanOrEqual(1)
  })
})
