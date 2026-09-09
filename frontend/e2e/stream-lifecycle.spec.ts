/**
 * PLA-501 (round 2, P5): the complete stream lifecycle in a real browser, on the real
 * transports.
 *
 * The containment and reveal suites each pin one axis; this one walks the life of a live
 * answer end to end, on the endpoints the app actually speaks:
 *
 *  - the tutor's `/api/sessions/{id}/chat` and the writer rail's
 *    `/api/drafts/{id}/chat/{session}` — the shared renderer's non-agent transports;
 *  - a Stop that lands mid-stream: the partial answer the reader watched is kept,
 *    settled, and the conversation re-enables;
 *  - an in-band error frame: the failure is surfaced on the turn's own row with its
 *    causal Retry, and the Retry re-sends over the replay transport and completes;
 *  - an empty stream: a turn that says nothing settles instead of hanging in streaming;
 *  - a hidden tab: while the tab's animation frames are withheld, the stream must still
 *    finish and its final text still publish — the terminal path owes no frame;
 *  - a selection the reader made in the live answer, carried through the settled handoff;
 *  - a reader who scrolls away mid-stream: the stream keeps its cadence and the pane
 *    does not yank the reader back to the tail;
 *  - a rapid next turn: the settled row keeps its identity under the new stream;
 *  - reduced motion: the cascade's fade is off, the words read as words, the turn
 *    settles.
 *
 * Everything is synthetic rows against the shared fixture at the network boundary, and a
 * real Node server that feeds the streaming endpoint with delayed frames.
 */
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import { expect, test, type Page } from '@playwright/test'

import {
  CLASS_ID,
  DRAFT_ID,
  SOLVER_SOLUTION_ID,
  TWIN_SESSION,
  installLyraApi,
} from './pla-504-math-fixture'

/** The app's baked API origin (VITE_API_BASE) — the real feed server must sit on it. */
const API_PORT = 8000

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
  'Access-Control-Allow-Headers': 'content-type, x-lyra-session',
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/** A synthetic session row, fresh IDs per scenario so the live store never sees a twin. */
function makeSession(id: number, title: string): Record<string, unknown> {
  return {
    id,
    class_id: CLASS_ID,
    title,
    mode: 'guide',
    artifact_part_id: null,
    created_at: '2026-09-08T10:00:00Z',
  }
}

function makeMessage(
  id: number,
  sessionId: number,
  role: 'user' | 'assistant',
  content: string,
  extra: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id,
    session_id: sessionId,
    role,
    content,
    thinking: '',
    thinking_ms: 0,
    retrieval_trimmed: false,
    omitted_document_count: 0,
    tool_activity: [],
    created_at: '2026-09-08T10:01:00Z',
    ...extra,
  }
}

/**
 * The deterministic feed: characters (2 at a time, 25 ms) for the first 60 characters,
 * words until character 300, then bursts of 40 characters at `burst` ms. The cadence is
 * deliberately slower than the reveal intervals, so the schedule visibly keeps up.
 */
function feedPlan(text: string, burst = 120): { chunk: string; delay: number }[] {
  const plan: { chunk: string; delay: number }[] = []
  let i = 0
  while (i < text.length) {
    if (i < 60) {
      const chunk = text.slice(i, i + 2)
      plan.push({ chunk, delay: 25 })
      i += chunk.length
    } else if (i < 300) {
      const space = text.indexOf(' ', i)
      const end = space === -1 ? text.length : space + 1
      plan.push({ chunk: text.slice(i, end), delay: 60 })
      i = end
    } else {
      const chunk = text.slice(i, i + 40)
      plan.push({ chunk, delay: burst })
      i += chunk.length
    }
  }
  return plan
}

function openSse(res: ServerResponse) {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    // Keep each synthetic stream's connection lifecycle independent.
    'Connection': 'close',
    ...CORS,
  })
}

function writeSse(res: ServerResponse, frame: unknown) {
  res.write(`data: ${JSON.stringify(frame)}\n\n`)
}

async function streamTokens(res: ServerResponse, text: string, burst = 120) {
  for (const { chunk, delay } of feedPlan(text, burst)) {
    writeSse(res, { type: 'token', text: chunk })
    await sleep(delay)
  }
}

function agentResult(messageId: number, text: string, stopped = 'completed') {
  return {
    type: 'result',
    result: {
      message_id: messageId,
      content: text,
      stopped,
      detail: '',
      activity: [],
      source_ids: [],
      workspace_change_ids: [],
      command_request_ids: [],
      profile_fact_ids: [],
    },
  }
}

const AGENT_ERROR_PAYLOAD = {
  type: 'error',
  status: 503,
  detail: 'The endpoint timed out.',
  retryable: true,
  stopped: 'error',
  activity: [],
  source_ids: [],
  workspace_change_ids: [],
  command_request_ids: [],
  profile_fact_ids: [],
}

/**
 * One feed server for a test: CORS preflights, a dispatch table of request handlers, and
 * the lifecycle callbacks the tests hang their persistence on.
 */
async function startServer(
  dispatch: (req: IncomingMessage, res: ServerResponse) => Promise<void> | void,
): Promise<Server> {
  const server = createServer((req, res) => {
    if (req.method === 'OPTIONS') {
      res.writeHead(204, CORS)
      res.end()
      return
    }
    void dispatch(req, res)
  })
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(API_PORT, '127.0.0.1', () => resolve())
  })
  return server
}

/**
 * The in-flight invariants, compact: revealed spans never re-hide (a resume dip reads a
 * hair below the finished value and is counted, never a drop toward hidden), pending
 * deadlines stay nondecreasing in reading order, and nothing above a unit fades. The
 * loop ends when the last `.assistant-content` holds no units — the turn has settled.
 */
async function watchUntilSettled(
  page: Page,
  { interval = 60, maxMs = 45_000 }: { interval?: number; maxMs?: number } = {},
) {
  const firstSeen = new Map<string, number>()
  let resumeDips = 0
  let samples = 0
  const start = Date.now()
  for (;;) {
    const snap = await page.evaluate(() => {
      const roots = Array.from(document.querySelectorAll<HTMLElement>('.assistant-content'))
      const root = roots[roots.length - 1]
      const units = root
        ? Array.from(root.querySelectorAll<HTMLElement>('[data-stream-word]')).map((u) => ({
            key: u.dataset.streamWord ?? '',
            at: u.dataset.streamRevealAt ? Number(u.dataset.streamRevealAt) : null,
            opacity: Number(getComputedStyle(u).opacity),
          }))
        : []
      const fadedAncestors = root
        ? Array.from(root.querySelectorAll('li, ul, ol, blockquote, p')).filter(
            (el) => Number(getComputedStyle(el).opacity) < 0.999,
          ).length
        : 0
      return { now: performance.now(), units, fadedAncestors }
    })
    samples += 1
    for (const unit of snap.units) {
      if (unit.at === null) continue
      const previouslyVisible = (firstSeen.get(unit.key) ?? 0) >= 0.99
      if (previouslyVisible && unit.opacity < 0.9) {
        throw new Error(`unit ${unit.key} re-hid at sample ${samples}: opacity ${unit.opacity}`)
      }
      if (previouslyVisible && unit.opacity < 0.99) resumeDips += 1
      if (unit.opacity > (firstSeen.get(unit.key) ?? 0)) firstSeen.set(unit.key, unit.opacity)
    }
    let prevAt = 0
    for (const unit of snap.units) {
      if (unit.at === null || unit.at <= snap.now) continue
      if (unit.at < prevAt) {
        throw new Error(
          `pending deadline out of reading order at sample ${samples}: ` +
            `a unit reveals at ${unit.at} ahead of ${prevAt}`,
        )
      }
      prevAt = unit.at
    }
    expect(snap.fadedAncestors, `faded ancestors at sample ${samples}`).toBe(0)
    if (snap.units.length === 0) break
    if (Date.now() - start > maxMs) throw new Error(`still streaming after ${maxMs}ms`)
    await sleep(interval)
  }
  expect(samples, 'the sampler ran no samples').toBeGreaterThan(1)
  return { samples, resumeDips }
}

/**
 * The text a reader sees in a rendered answer: the MathML KaTeX renders alongside its
 * HTML output duplicates the TeX source in `textContent`, so the visible text is read
 * with the MathML stripped. Raw TeX (`$...$`) must never appear in it.
 */
async function visibleAnswerText(page: Page, index = -1): Promise<string | null> {
  return page.evaluate((i) => {
    const roots = Array.from(document.querySelectorAll<HTMLElement>('.assistant-content'))
    const root = roots[i < 0 ? roots.length + i : i]
    if (!root) return null
    const clone = root.cloneNode(true) as HTMLElement
    for (const math of Array.from(clone.querySelectorAll('math'))) math.remove()
    return (clone.textContent ?? '').trim()
  }, index)
}

/** Let the streaming endpoint pass through to the real server; the fixture keeps the rest. */
async function bypassToServer(page: Page, ...patterns: string[]) {
  for (const pattern of patterns) {
    await page.route(pattern, (route) => route.continue())
  }
}

/** Send one message through the pane's composer. */
async function send(page: Page, text: string) {
  await page.getByLabel('Message Lyra').fill(text)
  await page.getByRole('button', { name: 'Send message' }).click()
}

const chatUrl = (sessionId: number) => `/#/classes/${CLASS_ID}/chat?session=${sessionId}`

test('the solver steps read through the shared renderer, and a step thread streams over the tutor transport', async ({
  page,
  browserName,
}) => {
  const newSessionId = 18
  const question = 'Why does the factorization give those poles?'
  const answer =
    'The denominator factors into $(s+2)(s+3)$, so the poles sit at $s=-2$ and $s=-3$. ' +
    'Each factor contributes one term to the partial fractions: the residue at each pole is ' +
    'one over the remaining factor, evaluated at that pole. That is the cover-up rule, and ' +
    'it is the reason the factorization is the whole of the work: once the poles and the ' +
    'residues are down, the transform is written out term by term, and the step above is ' +
    'the factorization that the cover-up rule reads off.'
  const sessionList = [
    makeSession(4, 'Poles and stability'),
    TWIN_SESSION,
    makeSession(newSessionId, 'Step questions'),
  ]
  const api = await installLyraApi(page, {}, { sessionId: newSessionId, initial: [] })
  let chatCalls = 0
  let retryAnswered = false
  const question2 = 'Is the residue at the slower pole the one read off first?'
  const answer2 =
    'Yes: the pole at $s=-2$ is the slower mode, and its residue is the first term ' +
    'the cover-up rule reads off, which is why the tail is light.'
  const server = await startServer(async (req, res) => {
    if (req.method === 'POST' && req.url === `/api/classes/${CLASS_ID}/sessions`) {
      // The thread's first question creates the session; the list gains the row.
      const json = (body: unknown) => {
        res.writeHead(200, { 'Content-Type': 'application/json', ...CORS })
        res.end(JSON.stringify(body))
      }
      json(makeSession(newSessionId, 'Step questions'))
      return
    }
    if (req.method === 'GET' && req.url === `/api/classes/${CLASS_ID}/sessions`) {
      const json = (body: unknown) => {
        res.writeHead(200, { 'Content-Type': 'application/json', ...CORS })
        res.end(JSON.stringify(body))
      }
      json(sessionList)
      return
    }
    if (req.method === 'POST' && req.url === `/api/sessions/${newSessionId}/chat`) {
      openSse(res)
      void (async () => {
        if (chatCalls === 0) {
          await streamTokens(res, answer, 160)
          await sleep(150)
          // The backend persists the turn as new rows with new IDs at the moment it answers.
          api.appendMessages!([
            makeMessage(181, newSessionId, 'user', question),
            makeMessage(182, newSessionId, 'assistant', answer),
          ])
          writeSse(res, { type: 'done', message_id: 182 })
        } else {
          // The second question fails in-band after a word: the attempt row persists the
          // failure, and the turn's error frame says what happened.
          for (const { chunk, delay } of feedPlan('The slower pole').slice(0, 2)) {
            writeSse(res, { type: 'token', text: chunk })
            await sleep(delay)
          }
          await sleep(150)
          // The attempt lives on the turn's own (user) row: the failure line renders
          // under it, exactly where the turn's question sits.
          api.appendMessages!([
            makeMessage(183, newSessionId, 'user', question2, {
              tutor_attempt: {
                state: 'failed',
                stopped_reason: 'error',
                detail: 'The tutor endpoint timed out.',
                operation_id: null,
              },
            }),
            makeMessage(184, newSessionId, 'assistant', ''),
          ])
          writeSse(res, { type: 'error', message: 'The tutor endpoint timed out.' })
        }
        res.end()
      })()
      chatCalls += 1
      return
    }
    if (req.method === 'POST' && req.url === `/api/sessions/${newSessionId}/retry`) {
      // The replay transport: the stored question is re-answered over /retry, and the
      // replacement settles under its own persisted ID.
      openSse(res)
      void (async () => {
        await streamTokens(res, answer2, 140)
        await sleep(150)
        // A cut client does not un-happen the durable answer: the row lands once,
        // whatever the reader's transport did with the stream.
        if (!retryAnswered) {
          api.appendMessages!([
            makeMessage(185, newSessionId, 'assistant', answer2, {
              tutor_attempt: {
                state: 'completed',
                stopped_reason: null,
                detail: null,
                operation_id: null,
              },
            }),
          ])
          retryAnswered = true
        }
        writeSse(res, { type: 'done', message_id: 185 })
        res.end()
      })()
      return
    }
    res.writeHead(200, { 'Content-Type': 'application/json', ...CORS })
    res.end('[]')
  })
  try {
    await bypassToServer(
      page,
      `**/api/classes/${CLASS_ID}/sessions`,
      `**/api/sessions/${newSessionId}/chat`,
      `**/api/sessions/${newSessionId}/retry`,
    )
    await page.goto(`/#/classes/${CLASS_ID}/solutions/${SOLVER_SOLUTION_ID}`)

    // The solver steps are the shared renderer at rest: the display equation inside the
    // list item is typeset, and no raw TeX survives anywhere on the page.
    await expect(page.locator('.assistant-content').first()).toBeVisible()
    const stepRender = await page.evaluate(() => {
      const clone = document.querySelector('main')?.cloneNode(true) as HTMLElement
      if (!clone) return { display: 0, raw: 0 }
      for (const math of Array.from(clone.querySelectorAll('math'))) math.remove()
      return {
        display: clone.querySelectorAll('.katex-display').length,
        raw:
          (clone.textContent ?? '').includes('$$') || (clone.textContent ?? '').includes('$\frac'),
      }
    })
    expect(stepRender.display, 'the step display equation did not typeset').toBeGreaterThanOrEqual(
      1,
    )
    expect(stepRender.raw, 'raw TeX printed on the solutions page').toBe(false)

    // Ask about a step: the thread opens under it and the answer streams over the tutor's
    // own transport - the shared renderer, live, settling onto its persisted rows.
    // The thread opens under the step; the solver steps share the renderer's content
    // class, so the stream assertions are scoped to the thread's own section.
    const thread = page.locator('section[aria-label^="Conversation about"]')
    await page.getByRole('button', { name: 'Ask about this step' }).first().click()
    await send(page, question)
    await expect(thread.locator('[data-stream-word]').first()).toBeAttached()
    await thread
      .locator('[data-stream-word]')
      .last()
      .waitFor({ state: 'detached', timeout: 60_000 })
    const settled = await thread.evaluate((section) => {
      const clone = section.cloneNode(true) as HTMLElement
      for (const math of Array.from(clone.querySelectorAll('math'))) math.remove()
      return (clone.textContent ?? '').trim()
    })
    expect(settled, 'the thread answer carried raw TeX').not.toContain('$')
    expect(settled).toContain('partial fractions')
    await expect.poll(() => api.servedMessages!().length, { timeout: 15_000 }).toBe(2)
    const served = api.servedMessages!()
    expect(served.map((row) => (row as { id: number }).id)).toEqual([181, 182])
    expect((served.at(-1) as { content: string }).content).toBe(answer)

    // Copy reads the settled answer back as its source: the clipboard carries the exact
    // content - TeX included - the row persists with. WebKit's context can grant
    // clipboard-read but no write permission at all (probed), so a programmatic
    // writeText there can never be authorized headless; the read-back is asserted where
    // the engine can prove it, and elsewhere the copy control is exercised without the
    // read-back.
    if (browserName !== 'webkit') {
      await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])
      await thread.getByRole('button', { name: 'Copy message' }).last().click()
      await expect
        .poll(async () => page.evaluate(() => navigator.clipboard.readText()), { timeout: 5_000 })
        .toBe(answer)
    } else {
      await thread.getByRole('button', { name: 'Copy message' }).last().click()
      // The button stays the row's control after the attempt: a rejected write must not
      // take the row or the thread down with it.
      await expect(thread.getByRole('button', { name: 'Copy message' }).last()).toBeVisible()
    }

    // The second question fails in-band, and the retry re-answers it over the replay
    // transport; the replacement settles under its own persisted ID.
    await send(page, question2)
    const tutorFailure = thread.locator('[data-tutor-turn-failure]')
    await expect(tutorFailure).toBeVisible()
    await expect(tutorFailure).toContainText('The tutor endpoint timed out.')
    // Wait for the retry's answer before checking reveal completion: no units can
    // also mean the stream has not begun. Exactly one click exercises the retry.
    await thread.getByRole('button', { name: 'Try again' }).last().click()
    const readThread = () =>
      thread.evaluate((section) => {
        const clone = section.cloneNode(true) as HTMLElement
        for (const math of Array.from(clone.querySelectorAll('math'))) math.remove()
        return (clone.textContent ?? '').trim()
      })
    await expect.poll(readThread, { timeout: 30_000 }).toContain('the tail is light')
    await expect(thread.locator('[data-stream-word]')).toHaveCount(0, { timeout: 30_000 })
    const settled2 = await readThread()
    expect(settled2, 'the retried answer carried raw TeX').not.toContain('$')
    expect(settled2).toContain('the tail is light')
    await expect.poll(() => api.servedMessages!().length, { timeout: 15_000 }).toBe(5)
    const served2 = api.servedMessages!()
    expect(served2.map((row) => (row as { id: number }).id)).toEqual([181, 182, 183, 184, 185])
    expect((served2.at(-1) as { content: string }).content).toBe(
      'Yes: the pole at $s=-2$ is the slower mode, and its residue is the first term ' +
        'the cover-up rule reads off, which is why the tail is light.',
    )
  } finally {
    server.close()
  }
})

test('the writer rail streams over its own transport and settles', async ({ page }) => {
  const draftSession = 5
  const question = 'What should the opening claim be?'
  const answer =
    'Open with the claim, then the transform that earns it.\n\n' +
    'The first paragraph should state $X(s)=\\frac{1}{s+2}$ and why it matters.'
  const api = await installLyraApi(
    page,
    {
      [`/api/sessions/${draftSession}/messages`]: [],
    },
    {
      sessionId: draftSession,
      initial: [],
    },
  )
  const server = await startServer(async (req, res) => {
    if (req.method === 'POST' && req.url === `/api/drafts/${DRAFT_ID}/chat/${draftSession}`) {
      openSse(res)
      void (async () => {
        await streamTokens(res, answer)
        await sleep(150)
        api.appendMessages!([
          makeMessage(89, draftSession, 'user', question, { draft_session_id: draftSession }),
          makeMessage(90, draftSession, 'assistant', answer, { draft_session_id: draftSession }),
        ])
        writeSse(res, { type: 'done', message_id: 90 })
        res.end()
      })()
      return
    }
    res.writeHead(200, { 'Content-Type': 'application/json', ...CORS })
    res.end('[]')
  })
  try {
    await bypassToServer(page, `**/api/drafts/${DRAFT_ID}/chat/${draftSession}`)
    await page.goto(`/#/classes/${CLASS_ID}/drafts/${DRAFT_ID}`)
    await send(page, question)

    await expect(
      page.locator('.assistant-content').last().locator('[data-stream-word]').first(),
    ).toBeAttached()
    await watchUntilSettled(page)

    const last = await visibleAnswerText(page)
    expect(last, 'the settled answer carries raw TeX').not.toContain('$')
    expect(last).toContain('why it matters')
    const served = api.servedMessages!()
    expect(served.map((row) => (row as { id: number }).id)).toEqual([89, 90])
    expect((served.at(-1) as { content: string }).content).toBe(answer)
  } finally {
    server.close()
  }
})

test('a stop mid-stream keeps the partial answer and re-enables the conversation', async ({
  page,
}) => {
  const sessionId = 9
  const question = 'Derive the impulse response, step by step.'
  const full =
    'Step one: factor the denominator, which puts the poles at $s=-2$ and $s=-3$. ' +
    'Step two: the cover-up rule gives the residues, one at each pole. Step three: each ' +
    'mode decays with its own rate, and the slowest one, $e^{-2t}$, decides the tail.'
  const partial = full.slice(0, full.indexOf('Step three:'))
  let stopped = false
  const api = await installLyraApi(
    page,
    {
      [`/api/classes/${CLASS_ID}/sessions`]: [
        makeSession(4, 'Poles and stability'),
        TWIN_SESSION,
        makeSession(sessionId, 'Stops'),
      ],
      [`/api/sessions/${sessionId}`]: makeSession(sessionId, 'Stops'),
      [`/api/classes/${CLASS_ID}/sessions/${sessionId}/agent/access-dismissals`]: {
        dismissals: [],
      },
    },
    { sessionId, initial: [] },
  )
  const server = await startServer(async (req, res) => {
    if (
      req.method === 'POST' &&
      req.url === `/api/classes/${CLASS_ID}/sessions/${sessionId}/agent-chat/stop`
    ) {
      // The backend durably settles the stopped turn at the moment it inspects it.
      stopped = true
      api.appendMessages!([
        makeMessage(91, sessionId, 'user', question, {
          agent_attempt: {
            state: 'stopped',
            stopped_reason: 'stopped',
            detail: null,
            operation_id: null,
          },
        }),
        makeMessage(92, sessionId, 'assistant', partial, {
          agent_attempt: {
            state: 'stopped',
            stopped_reason: 'stopped',
            detail: null,
            operation_id: null,
          },
        }),
      ])
      res.writeHead(200, { 'Content-Type': 'application/json', ...CORS })
      res.end(JSON.stringify({ stopped: true, settling: false }))
      return
    }
    if (
      req.method === 'POST' &&
      req.url === `/api/classes/${CLASS_ID}/sessions/${sessionId}/agent-chat`
    ) {
      openSse(res)
      // A slow stream the reader can catch mid-answer.
      for (const { chunk, delay } of feedPlan(full, 200)) {
        writeSse(res, { type: 'token', text: chunk })
        await sleep(delay)
        if (stopped) break
      }
      res.end()
      return
    }
    res.writeHead(200, { 'Content-Type': 'application/json', ...CORS })
    res.end('[]')
  })
  try {
    await bypassToServer(
      page,
      `**/api/classes/${CLASS_ID}/sessions/${sessionId}/agent-chat`,
      `**/api/classes/${CLASS_ID}/sessions/${sessionId}/agent-chat/stop`,
    )
    await page.goto(chatUrl(sessionId))
    await send(page, question)

    await expect(
      page.locator('.assistant-content').last().locator('[data-stream-word]').first(),
    ).toBeAttached()
    // Let a few words land, then stop.
    await page.waitForTimeout(900)
    await page.getByRole('button', { name: 'Stop generating' }).click()

    // The partial the reader watched is what they keep: it settles to the static render
    // of exactly that text, the stream markers are gone, and the conversation is free.
    await page
      .locator('.assistant-content')
      .last()
      .filter({ has: page.locator('[data-stream-word]') })
      .waitFor({ state: 'detached', timeout: 15_000 })
    const last = (await visibleAnswerText(page)) ?? ''
    expect(last).not.toContain('$')
    expect(last).toContain('the cover-up rule gives the residues')
    expect(await page.locator('[data-stream-word]').count()).toBe(0)
    const served = api.servedMessages!()
    expect((served.at(-1) as { content: string }).content).toBe(partial)
    const composer = page.getByLabel('Message Lyra')
    await expect(composer).toBeEnabled()
    expect(served.map((row) => (row as { id: number }).id)).toEqual([91, 92])
  } finally {
    server.close()
  }
})

test('an error frame fails the turn on its own row, and the retry re-sends over the replay transport', async ({
  page,
}) => {
  const sessionId = 10
  const question = 'Explain the region of convergence.'
  const goodAnswer =
    'The region of convergence is the half-plane $\\Re(s) > -2$.\n\n' +
    'It is bounded by the rightmost pole, $s=-2$, and the series converges to the right of it.'
  const api = await installLyraApi(
    page,
    {
      [`/api/classes/${CLASS_ID}/sessions`]: [
        makeSession(4, 'Poles and stability'),
        TWIN_SESSION,
        makeSession(sessionId, 'Errors and retry'),
      ],
      [`/api/sessions/${sessionId}`]: makeSession(sessionId, 'Errors and retry'),
      [`/api/classes/${CLASS_ID}/sessions/${sessionId}/agent/access-dismissals`]: {
        dismissals: [],
      },
    },
    { sessionId, initial: [] },
  )
  let firstAttempt = true
  let retryAnswered = false
  const server = await startServer(async (req, res) => {
    const base = `/api/classes/${CLASS_ID}/sessions/${sessionId}/agent-chat`
    if (req.method === 'POST' && (req.url === base || req.url === `${base}/retry`)) {
      const retry = req.url === `${base}/retry`
      // The user row persists when the attempt is accepted; the outcome row lands when
      // the attempt settles.
      if (firstAttempt) {
        api.appendMessages!([
          makeMessage(101, sessionId, 'user', question, {
            agent_attempt: {
              state: 'failed',
              stopped_reason: 'error',
              detail: 'The endpoint timed out.',
              operation_id: null,
            },
          }),
        ])
      }
      openSse(res)
      if (firstAttempt && !retry) {
        // Three words in, then the in-band failure.
        for (const { chunk, delay } of feedPlan('The region of convergence is').slice(0, 5)) {
          writeSse(res, { type: 'token', text: chunk })
          await sleep(delay)
        }
        await sleep(150)
        api.appendMessages!([
          makeMessage(102, sessionId, 'assistant', '', {
            agent_attempt: {
              state: 'failed',
              stopped_reason: 'error',
              detail: 'The endpoint timed out.',
              operation_id: null,
            },
          }),
        ])
        writeSse(res, AGENT_ERROR_PAYLOAD)
        res.end()
        firstAttempt = false
        return
      }
      firstAttempt = false
      await streamTokens(res, goodAnswer)
      await sleep(150)
      // A cut client does not un-happen the durable answer: the row lands once, whatever
      // the reader's transport did with the stream.
      if (!retryAnswered) {
        api.appendMessages!([
          makeMessage(104, sessionId, 'assistant', goodAnswer, {
            agent_attempt: {
              state: 'completed',
              stopped_reason: null,
              detail: null,
              operation_id: null,
            },
          }),
        ])
        retryAnswered = true
      }
      writeSse(res, agentResult(104, goodAnswer))
      res.end()
      return
    }
    res.writeHead(200, { 'Content-Type': 'application/json', ...CORS })
    res.end('[]')
  })
  try {
    await bypassToServer(
      page,
      `**/api/classes/${CLASS_ID}/sessions/${sessionId}/agent-chat`,
      `**/api/classes/${CLASS_ID}/sessions/${sessionId}/agent-chat/retry`,
    )
    await page.goto(chatUrl(sessionId))
    await send(page, question)

    // The failure lands on the turn's own row with its detail and its causal retry.
    const failure = page.locator('[data-agent-turn-failure]')
    await expect(failure).toBeVisible()
    await expect(failure).toContainText('The endpoint timed out.')
    const retry = page.getByRole('button', { name: 'Try again' })
    await expect(retry).toBeVisible()

    // The retry re-sends over the replay transport and completes: the answer settles onto
    // the static render of the good content, under the persisted ID.
    await page.getByRole('button', { name: 'Try again' }).last().click()
    await expect
      .poll(() => visibleAnswerText(page), { timeout: 30_000 })
      .toContain('converges to the right of it')
    await expect(page.locator('.assistant-content [data-stream-word]')).toHaveCount(0, {
      timeout: 30_000,
    })
    const last = (await visibleAnswerText(page)) ?? ''
    expect(last, 'the retried answer carries raw TeX').not.toContain('$')
    expect(last).toContain('converges to the right of it')
    const served = api.servedMessages!()
    expect(served.map((row) => (row as { id: number }).id)).toEqual([101, 102, 104])
    expect((served.at(-1) as { content: string }).content).toBe(goodAnswer)
  } finally {
    server.close()
  }
})

test('an empty stream settles instead of hanging in streaming', async ({ page }) => {
  const sessionId = 11
  const question = 'Anything to add?'
  const api = await installLyraApi(
    page,
    {
      [`/api/classes/${CLASS_ID}/sessions`]: [
        makeSession(4, 'Poles and stability'),
        TWIN_SESSION,
        makeSession(sessionId, 'Empty turns'),
      ],
      [`/api/sessions/${sessionId}`]: makeSession(sessionId, 'Empty turns'),
      [`/api/classes/${CLASS_ID}/sessions/${sessionId}/agent/access-dismissals`]: {
        dismissals: [],
      },
    },
    { sessionId, initial: [] },
  )
  const server = await startServer(async (req, res) => {
    if (
      req.method === 'POST' &&
      req.url === `/api/classes/${CLASS_ID}/sessions/${sessionId}/agent-chat`
    ) {
      openSse(res)
      await sleep(400)
      // Zero tokens: the turn says nothing, and its terminal still arrives.
      api.appendMessages!([
        makeMessage(111, sessionId, 'user', question),
        makeMessage(112, sessionId, 'assistant', ''),
      ])
      writeSse(res, agentResult(112, ''))
      res.end()
      return
    }
    res.writeHead(200, { 'Content-Type': 'application/json', ...CORS })
    res.end('[]')
  })
  try {
    await bypassToServer(page, `**/api/classes/${CLASS_ID}/sessions/${sessionId}/agent-chat`)
    await page.goto(chatUrl(sessionId))
    await send(page, question)

    // The turn settles: no stream markers left anywhere, the conversation is free again.
    await expect(page.locator('[data-stream-word]')).toHaveCount(0, { timeout: 30_000 })
    const composer = page.getByLabel('Message Lyra')
    await expect(composer).toBeEnabled()
    // The settle's refetch lands shortly after; wait for the wire to show both rows.
    await expect.poll(() => api.servedMessages!().length, { timeout: 15_000 }).toBe(2)
    const served = api.servedMessages!()
    expect(served.map((row) => (row as { id: number }).id)).toEqual([111, 112])
  } finally {
    server.close()
  }
})

test('a hidden tab still publishes the terminal text without owing a frame', async ({
  browser,
  page,
}) => {
  const sessionId = 12
  const question = 'Summarize the two modes.'
  const answer =
    'Two modes: the slow one, $e^{-2t}$, with weight one, and the fast one, $2e^{-3t}$, ' +
    'which is why the tail is light. The slowest pole decides what you see last.'
  const api = await installLyraApi(
    page,
    {
      [`/api/classes/${CLASS_ID}/sessions`]: [
        makeSession(4, 'Poles and stability'),
        TWIN_SESSION,
        makeSession(sessionId, 'Hidden tabs'),
      ],
      [`/api/sessions/${sessionId}`]: makeSession(sessionId, 'Hidden tabs'),
      [`/api/classes/${CLASS_ID}/sessions/${sessionId}/agent/access-dismissals`]: {
        dismissals: [],
      },
    },
    { sessionId, initial: [] },
  )
  const server = await startServer(async (req, res) => {
    if (
      req.method === 'POST' &&
      req.url === `/api/classes/${CLASS_ID}/sessions/${sessionId}/agent-chat`
    ) {
      openSse(res)
      void (async () => {
        await streamTokens(res, answer, 160)
        await sleep(200)
        api.appendMessages!([
          makeMessage(121, sessionId, 'user', question),
          makeMessage(122, sessionId, 'assistant', answer),
        ])
        writeSse(res, agentResult(122, answer))
        res.end()
      })()
      return
    }
    res.writeHead(200, { 'Content-Type': 'application/json', ...CORS })
    res.end('[]')
  })
  try {
    await bypassToServer(page, `**/api/classes/${CLASS_ID}/sessions/${sessionId}/agent-chat`)
    await page.goto(chatUrl(sessionId))
    await send(page, question)
    await expect(
      page.locator('.assistant-content').last().locator('[data-stream-word]').first(),
    ).toBeAttached()

    // A second page takes the front: this tab's animation frames are withheld while the
    // stream keeps arriving. The terminal must publish without a frame.
    const other = await browser.newPage()
    await other.goto('about:blank')
    await other.bringToFront()
    await page.waitForTimeout(1_500)
    await page.bringToFront()

    // Back in front: the whole answer is on screen, settled, nothing stranded.
    await page
      .locator('.assistant-content')
      .last()
      .filter({ has: page.locator('[data-stream-word]') })
      .waitFor({ state: 'detached', timeout: 30_000 })
    const last = (await visibleAnswerText(page)) ?? ''
    expect(last, 'the hidden tab left the answer raw or short').not.toContain('$')
    expect(last).toContain('The slowest pole decides what you see last')
    await other.close()
  } finally {
    server.close()
  }
})

test('a selection made in the live answer carries through the settled handoff', async ({
  page,
}) => {
  const sessionId = 13
  const question = 'What are the two weights?'
  const answer =
    'The weights are one and minus two: the first mode carries one, and the second ' +
    'carries minus two, so the tail is light. The residue at each pole is the weight.'
  const api = await installLyraApi(
    page,
    {
      [`/api/classes/${CLASS_ID}/sessions`]: [
        makeSession(4, 'Poles and stability'),
        TWIN_SESSION,
        makeSession(sessionId, 'Selections'),
      ],
      [`/api/sessions/${sessionId}`]: makeSession(sessionId, 'Selections'),
      [`/api/classes/${CLASS_ID}/sessions/${sessionId}/agent/access-dismissals`]: {
        dismissals: [],
      },
    },
    { sessionId, initial: [] },
  )
  const server = await startServer(async (req, res) => {
    if (
      req.method === 'POST' &&
      req.url === `/api/classes/${CLASS_ID}/sessions/${sessionId}/agent-chat`
    ) {
      openSse(res)
      void (async () => {
        await streamTokens(res, answer)
        await sleep(150)
        api.appendMessages!([
          makeMessage(131, sessionId, 'user', question),
          makeMessage(132, sessionId, 'assistant', answer),
        ])
        writeSse(res, agentResult(132, answer))
        res.end()
      })()
      return
    }
    res.writeHead(200, { 'Content-Type': 'application/json', ...CORS })
    res.end('[]')
  })
  try {
    await bypassToServer(page, `**/api/classes/${CLASS_ID}/sessions/${sessionId}/agent-chat`)
    await page.goto(chatUrl(sessionId))
    await send(page, question)

    // While the answer is still live, select a phrase across its first words.
    await page.waitForFunction(() => {
      const root = Array.from(document.querySelectorAll<HTMLElement>('.assistant-content')).at(-1)
      if (!root) return false
      const units = root.querySelectorAll('[data-stream-word]')
      return units.length >= 3 && (root.textContent ?? '').includes('weights are')
    })
    const selectionText = await page.evaluate(() => {
      const root = Array.from(document.querySelectorAll<HTMLElement>('.assistant-content')).at(-1)!
      const units = Array.from(root.querySelectorAll<HTMLElement>('[data-stream-word]'))
      // Two full words: the range spans their text nodes and the space between.
      const first = units[0]!.firstChild
      const second = units[1]!.firstChild
      if (!(first instanceof Text) || !(second instanceof Text)) return null
      const range = document.createRange()
      range.setStart(first, 0)
      range.setEnd(second, Math.min(4, second.length))
      const selection = window.getSelection()
      selection?.removeAllRanges()
      selection?.addRange(range)
      return selection?.toString() ?? null
    })
    expect(selectionText, 'no selection could be made in the live answer').toBeTruthy()

    // The turn settles onto the static render of the same text; the selection must come
    // back onto it, not die with the streaming row.
    await page
      .locator('.assistant-content')
      .last()
      .filter({ has: page.locator('[data-stream-word]') })
      .waitFor({ state: 'detached', timeout: 30_000 })
    await page.waitForFunction(() => (window.getSelection()?.toString() ?? '') !== '', null, {
      timeout: 15_000,
    })
    const after = await page.evaluate(() => {
      const selection = window.getSelection()!
      const root = Array.from(document.querySelectorAll<HTMLElement>('.assistant-content')).at(-1)!
      return {
        text: selection.toString(),
        anchorInsideSettledAnswer:
          selection.anchorNode != null &&
          selection.focusNode != null &&
          root.contains(selection.anchorNode) &&
          root.contains(selection.focusNode),
      }
    })
    expect(after.text).toBe(selectionText)
    expect(after.anchorInsideSettledAnswer, 'the selection left the settled answer').toBe(true)
  } finally {
    server.close()
  }
})

test('a reader who scrolls away keeps their place while the stream runs', async ({ page }) => {
  const sessionId = 14
  const question = 'Walk through every step of the transform.'
  // Long enough to outgrow the conversation's viewport, so a scroll to the top is a real
  // scroll-away rather than a no-op on content that fits.
  const steps = [
    'write the transfer function $H(s)=\\frac{s+1}{(s+2)(s+3)}$ and factor the denominator, which puts the poles at $s=-2$ and $s=-3$',
    'apply the cover-up rule to the residues, one at each pole, and note that the residue at $s=-2$ is the slowest one',
    'let each mode decay with its own rate, since the slowest one, $e^{-2t}$, decides the tail of the response',
    'check the final value against the table, and confirm that the table agrees with the transform',
    'verify that the damping ratio stays under one, so the response settles without oscillation',
    'read the steady-state row of the table, which is the last thing the reader should see',
    'compare the two modes side by side and name the one that lingers on the plot',
    'state the region of convergence, the half-plane to the right of the rightmost pole',
    'close with the single sentence a grader would write in the margin of the work',
  ]
  const answer = steps.map((s, i) => `Step ${i + 1}: ${s}.`).join(' ')
  const api = await installLyraApi(
    page,
    {
      [`/api/classes/${CLASS_ID}/sessions`]: [
        makeSession(4, 'Poles and stability'),
        TWIN_SESSION,
        makeSession(sessionId, 'Scrolling readers'),
      ],
      [`/api/sessions/${sessionId}`]: makeSession(sessionId, 'Scrolling readers'),
      [`/api/classes/${CLASS_ID}/sessions/${sessionId}/agent/access-dismissals`]: {
        dismissals: [],
      },
    },
    { sessionId, initial: [] },
  )
  const server = await startServer(async (req, res) => {
    if (
      req.method === 'POST' &&
      req.url === `/api/classes/${CLASS_ID}/sessions/${sessionId}/agent-chat`
    ) {
      openSse(res)
      void (async () => {
        await streamTokens(res, answer, 160)
        await sleep(150)
        api.appendMessages!([
          makeMessage(141, sessionId, 'user', question),
          makeMessage(142, sessionId, 'assistant', answer),
        ])
        writeSse(res, agentResult(142, answer))
        res.end()
      })()
      return
    }
    res.writeHead(200, { 'Content-Type': 'application/json', ...CORS })
    res.end('[]')
  })
  try {
    await bypassToServer(page, `**/api/classes/${CLASS_ID}/sessions/${sessionId}/agent-chat`)
    await page.goto(chatUrl(sessionId))
    await send(page, question)

    await expect(
      page.locator('.assistant-content').last().locator('[data-stream-word]').first(),
    ).toBeAttached()
    // The content has to outgrow the viewport before a scroll-away is real.
    await page.waitForFunction(
      () => {
        const root = Array.from(document.querySelectorAll<HTMLElement>('.assistant-content')).at(-1)
        return (root?.textContent ?? '').length > 500
      },
      null,
      { timeout: 30_000 },
    )
    // The reader leaves the tail. A wheel event first, then the scroll: the pane only
    // stops following a scroll the reader drove, and a bare programmatic scrollTo would
    // not be one — the pane would keep following and yank the position back on every token.
    const viewport = page.locator('[data-slot="scroll-area-viewport"]').last()
    await viewport.evaluate((node) => {
      node.dispatchEvent(new WheelEvent('wheel', { deltaY: -120, bubbles: true }))
      node.scrollTo({ top: 0, behavior: 'instant' })
    })

    // While the stream keeps growing, the reader's place holds: the viewport stays near
    // the top, and the in-flight invariants still hold on the growing answer.
    await watchUntilSettled(page, { interval: 90 })
    const scroll = await viewport.evaluate((node) => node.scrollTop)
    expect(scroll, 'the pane yanked the reader back to the tail mid-stream').toBeLessThan(300)

    // Settled: the full answer is there, one row, under its persisted ID.
    const last = (await visibleAnswerText(page)) ?? ''
    expect(last, 'the scrolled-away stream carried raw TeX').not.toContain('$')
    expect(last).toContain('grader would write in the margin')
    const served = api.servedMessages!()
    expect(served.map((row) => (row as { id: number }).id)).toEqual([141, 142])
    expect((served.at(-1) as { content: string }).content).toBe(answer)
  } finally {
    server.close()
  }
})

test('a rapid next turn keeps the settled row under it', async ({ page }) => {
  const sessionId = 15
  const api = await installLyraApi(
    page,
    {
      [`/api/classes/${CLASS_ID}/sessions`]: [
        makeSession(4, 'Poles and stability'),
        TWIN_SESSION,
        makeSession(sessionId, 'Rapid turns'),
      ],
      [`/api/sessions/${sessionId}`]: makeSession(sessionId, 'Rapid turns'),
      [`/api/classes/${CLASS_ID}/sessions/${sessionId}/agent/access-dismissals`]: {
        dismissals: [],
      },
    },
    { sessionId, initial: [] },
  )
  let turn = 0
  const answers = [
    'The first answer: the pole at $s=-2$ is the slow mode.',
    'The second answer: the pole at $s=-3$ is the fast mode.',
  ]
  const ids = [
    [151, 152],
    [153, 154],
  ]
  const server = await startServer(async (req, res) => {
    if (
      req.method === 'POST' &&
      req.url === `/api/classes/${CLASS_ID}/sessions/${sessionId}/agent-chat`
    ) {
      openSse(res)
      const n = turn++
      void (async () => {
        await streamTokens(res, answers[n]!, 100)
        await sleep(100)
        const [qid, aid] = ids[n]!
        api.appendMessages!([
          makeMessage(
            qid,
            sessionId,
            'user',
            n === 0 ? 'What is the slow mode?' : 'And the fast one?',
          ),
          makeMessage(aid, sessionId, 'assistant', answers[n]!),
        ])
        writeSse(res, agentResult(aid, answers[n]!))
        res.end()
      })()
      return
    }
    res.writeHead(200, { 'Content-Type': 'application/json', ...CORS })
    res.end('[]')
  })
  try {
    await bypassToServer(page, `**/api/classes/${CLASS_ID}/sessions/${sessionId}/agent-chat`)
    await page.goto(chatUrl(sessionId))

    // Turn one.
    await send(page, 'What is the slow mode?')
    await expect(
      page.locator('.assistant-content').last().locator('[data-stream-word]').first(),
    ).toBeAttached()
    await page
      .locator('.assistant-content')
      .last()
      .filter({ has: page.locator('[data-stream-word]') })
      .waitFor({ state: 'detached', timeout: 30_000 })
    await expect(page.getByLabel('Message Lyra')).toBeEnabled()

    // Mark the settled row's node: identity is read off the DOM, not off an index.
    await page.evaluate(() => {
      const root = Array.from(document.querySelectorAll<HTMLElement>('.assistant-content')).at(-1)!
      ;(root as HTMLElement & { __mark?: number }).__mark = 1
    })

    // Turn two, immediately: while the new answer streams, the settled row's node must
    // still be the same node — a remount would drop the mark.
    await send(page, 'And the fast one?')
    await expect(
      page.locator('.assistant-content').last().locator('[data-stream-word]').first(),
    ).toBeAttached()
    const kept = await page.evaluate(() => {
      const roots = Array.from(document.querySelectorAll<HTMLElement>('.assistant-content'))
      return roots[0] ? ((roots[0] as HTMLElement & { __mark?: number }).__mark ?? 0) : 0
    })
    expect(kept, 'the settled row was remounted by the next turn').toBe(1)

    // Settled: both answers, in order, and the marked row is still the first one.
    await page
      .locator('.assistant-content')
      .last()
      .filter({ has: page.locator('[data-stream-word]') })
      .waitFor({ state: 'detached', timeout: 30_000 })
    const rows = await page.evaluate(() =>
      Array.from(document.querySelectorAll<HTMLElement>('.assistant-content')).map((node) => {
        const clone = node.cloneNode(true) as HTMLElement
        for (const math of Array.from(clone.querySelectorAll('math'))) math.remove()
        return {
          text: (clone.textContent ?? '').trim(),
          marked: (node as HTMLElement & { __mark?: number }).__mark ?? 0,
        }
      }),
    )
    expect(rows.length).toBe(2)
    expect(rows[0]!.text).toContain('the slow mode')
    expect(rows[0]!.text).not.toContain('$')
    expect(rows[0]!.marked).toBe(1)
    expect(rows[1]!.text).toContain('the fast mode')
    expect(rows[1]!.text).not.toContain('$')
    const served = api.servedMessages!()
    expect(served.map((row) => (row as { id: number }).id)).toEqual([151, 152, 153, 154])
  } finally {
    server.close()
  }
})

test('reduced motion reads the words as words and still settles', async ({ browser }) => {
  const sessionId = 16
  const question = 'State the two weights.'
  const answer =
    'The weights are one and minus two: the first mode carries one, and the second ' +
    'carries minus two, so the tail is light.'
  const context = await browser.newContext({ reducedMotion: 'reduce' })
  const reduced = await context.newPage()
  const api = await installLyraApi(
    reduced,
    {
      [`/api/classes/${CLASS_ID}/sessions`]: [
        makeSession(4, 'Poles and stability'),
        TWIN_SESSION,
        makeSession(sessionId, 'Reduced motion'),
      ],
      [`/api/sessions/${sessionId}`]: makeSession(sessionId, 'Reduced motion'),
      [`/api/classes/${CLASS_ID}/sessions/${sessionId}/agent/access-dismissals`]: {
        dismissals: [],
      },
    },
    { sessionId, initial: [] },
  )
  const server = await startServer(async (req, res) => {
    if (
      req.method === 'POST' &&
      req.url === `/api/classes/${CLASS_ID}/sessions/${sessionId}/agent-chat`
    ) {
      openSse(res)
      void (async () => {
        await streamTokens(res, answer)
        await sleep(150)
        api.appendMessages!([
          makeMessage(161, sessionId, 'user', question),
          makeMessage(162, sessionId, 'assistant', answer),
        ])
        writeSse(res, agentResult(162, answer))
        res.end()
      })()
      return
    }
    res.writeHead(200, { 'Content-Type': 'application/json', ...CORS })
    res.end('[]')
  })
  try {
    await bypassToServer(reduced, `**/api/classes/${CLASS_ID}/sessions/${sessionId}/agent-chat`)
    await reduced.goto(chatUrl(sessionId))
    await send(reduced, question)

    await expect(
      reduced.locator('.assistant-content').last().locator('[data-stream-word]').first(),
    ).toBeAttached()

    // While words are still revealing, none of them is hidden: the fade is off, so every
    // unit reads at full opacity from the moment it lands.
    const minimum = await reduced
      .locator('.assistant-content')
      .last()
      .evaluate((root) =>
        Math.min(
          ...Array.from(root.querySelectorAll<HTMLElement>('[data-stream-word]')).map((u) =>
            Number(getComputedStyle(u).opacity),
          ),
        ),
      )
    expect(minimum, 'a word revealed below full opacity under reduced motion').toBe(1)

    await reduced
      .locator('.assistant-content')
      .last()
      .filter({ has: reduced.locator('[data-stream-word]') })
      .waitFor({ state: 'detached', timeout: 30_000 })
    const last = await reduced.evaluate(() => {
      const root = Array.from(document.querySelectorAll<HTMLElement>('.assistant-content')).at(-1)
      if (!root) return null
      const clone = root.cloneNode(true) as HTMLElement
      for (const math of Array.from(clone.querySelectorAll('math'))) math.remove()
      return (clone.textContent ?? '').trim()
    })
    expect(last).toBe(answer.trim())
    await context.close()
  } finally {
    server.close()
  }
})
