/**
 * Stop during a real tool dispatch (PLA-401 final pass, item 7) — the real UI, the real
 * backend, and a real dispatch held at the acceptance barrier:
 *
 *  1. A scripted agent turn calls `search_web`; the tool-dispatch barrier holds the
 *     worker INSIDE that actual dispatch.
 *  2. The student clicks the real Stop. The UI enters the bounded "Stopping…" state and
 *     does not present the turn as stopped until the server has confirmed it.
 *  3. The worker leaves; the server confirms; the turn settles as stopped - the durable
 *     attempt is `stopped`, no reply was stored, the in-flight search produced no later
 *     durable effect, and the session is free: the next message round-trips.
 *
 * The backend test suite proves the same contract against a unit-stopped dispatch
 * (`test_agent_chat_concurrency.py`); this spec proves it through the product's actual
 * buttons and its actual dispatch path.
 */

import { test, expect } from '@playwright/test'
import {
  apiGet,
  apiPut,
  clearTutorState,
  createClass,
  createSession,
  enableToolBarrier,
  enqueueTutorResponse,
  getTutorRequests,
  releaseToolBarrier,
  sendChatMessage,
  waitForChatResponse,
  waitForToolBarrier,
} from './helpers'

function toolCallCompletion(name: string, args: unknown): Record<string, unknown> {
  return {
    id: `chatcmpl-stop-${Date.now()}-${Math.random().toString(36).slice(2)}`,
    object: 'chat.completion',
    choices: [
      {
        index: 0,
        message: {
          role: 'assistant',
          content: null,
          tool_calls: [
            {
              id: `call_stop_${Math.random().toString(36).slice(2)}`,
              type: 'function',
              function: { name, arguments: JSON.stringify(args) },
            },
          ],
        },
        finish_reason: 'tool_calls',
      },
    ],
    usage: { prompt_tokens: 100, completion_tokens: 40, total_tokens: 140 },
  }
}

test.describe('Stop during a real tool dispatch (PLA-401 final pass)', () => {
  test.describe.configure({ timeout: 180_000 })

  let classId: number
  let originalModel: string

  test.beforeAll(async () => {
    const cls = await createClass('Acceptance: Stop During Dispatch')
    classId = cls.id
    // This class is granted public web research: the turn is offered `search_web`, and
    // its dispatch is the worker the Stop must handle. The grant is run-scoped:
    // afterAll restores the fresh-database default.
    await apiPut('/api/settings', { allow_web_research: true })
    // The specs before this one in the suite have already run the solver's capability
    // probe against the scripted endpoint: the fixture answers the probe in plain text,
    // so the measured verdict `tools_supported = false` is now stored for this endpoint.
    // The product's own mechanism for discarding a verdict that no longer describes the
    // configured endpoint is a settings change - endpoint URL or model - which resets the
    // probe results to unknown. Renaming the model (the fixture answers whatever name it
    // is called with) gives this spec a clean capability state and lets the turn plan the
    // tool surface it scripts; the original model is restored in afterAll.
    const settingsRes = await apiGet('/api/settings')
    const settings = (await settingsRes.json()) as { model: string | null }
    originalModel = settings.model ?? 'test-model'
    await apiPut('/api/settings', { model: `test-model-${Date.now()}` })
  })

  test.afterAll(async () => {
    await apiPut('/api/settings', { model: originalModel, allow_web_research: false })
  })

  test.afterEach(async () => {
    await clearTutorState()
  })

  test('Stop waits for server confirmation, then frees the session without later effects', async ({
    page,
  }) => {
    const session = await createSession(classId)
    await page.goto(`/classes/${classId}/chat?session=${session.id}`)
    await page.waitForLoadState('networkidle')

    // Round one: the model asks for a web search. Round two: the terminal answer -
    // reached only if the turn is NOT stopped (it is not: the Stop lands mid-dispatch,
    // and a stopped turn makes no further model call).
    await enqueueTutorResponse({
      raw: toolCallCompletion('search_web', { query: 'definition of convolution' }),
    })
    await enqueueTutorResponse({
      content: 'Convolution slides one signal past the other and sums the overlap at each step.',
    })

    // The dispatch the Stop is about: hold the worker inside its real search_web call.
    await enableToolBarrier()
    await sendChatMessage(page, 'Research the definition of convolution.')
    await waitForToolBarrier(30_000)

    // The real Stop: a round trip to the server. While the worker is still inside its
    // dispatch, the UI shows the bounded "Stopping…" state - it does not claim the turn
    // is stopped before the server has confirmed it.
    await page.getByRole('button', { name: 'Stop generating' }).click()
    await expect(page.getByRole('button', { name: 'Stopping…' })).toBeVisible()

    // The worker leaves: the server confirms, and only then does the turn settle.
    await releaseToolBarrier()
    await expect(page.getByRole('button', { name: 'Stopping…' })).toHaveCount(0)
    await waitForChatResponse(page, 60_000)
    // The transcript shows the stopped turn honestly - the failure line under the
    // question, with its causal Retry - and no answer was ever produced.
    const failure = page.locator('[data-agent-turn-failure]')
    await expect(failure).toBeVisible()
    await expect(failure.getByRole('button', { name: 'Try again' })).toBeVisible()
    await expect(page.locator('[data-role="assistant"]')).toHaveCount(0)

    // The durable state says the same: the attempt settled as stopped, with no reply
    // and no later durable effect from the in-flight search.
    const messagesRes = await apiGet(`/api/sessions/${session.id}/messages`)
    expect(messagesRes.status).toBe(200)
    const messages = (await messagesRes.json()) as Array<{
      role: string
      agent_attempt?: { state: string } | null
    }>
    expect(messages.filter((m) => m.role === 'user')).toHaveLength(1)
    expect(messages.filter((m) => m.role === 'user')[0].agent_attempt?.state).toBe('stopped')
    expect(messages.some((m) => m.role === 'assistant')).toBe(false)

    // The session is free: the next message round-trips normally through the real UI.
    await sendChatMessage(page, 'What is a Fourier transform?')
    await waitForChatResponse(page, 60_000)
    const secondRes = await apiGet(`/api/sessions/${session.id}/messages`)
    const second = (await secondRes.json()) as Array<{
      role: string
      content: string
      agent_attempt?: { state: string } | null
    }>
    const users = second.filter((m) => m.role === 'user')
    expect(users).toHaveLength(2)
    expect(users[1].content).toBe('What is a Fourier transform?')
    expect(users[1].agent_attempt?.state).toBe('completed')
    const assistant = second.filter((m) => m.role === 'assistant')
    expect(assistant).toHaveLength(1)
    expect(assistant[0].content).toContain('Convolution slides one signal')
    // The stopped turn made exactly one model call (its round one); the new turn made the
    // next one - nothing ran between the Stop and the new question.
    const requests = await getTutorRequests()
    expect(requests).toHaveLength(2)
  })
})
