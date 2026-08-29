/**
 * Shared helpers for acceptance specs.
 *
 * Every helper communicates with the running stack over HTTP -- no mocks, no
 * monkeypatches, no intercepted routes.  The only "control" API is the tutor
 * fixture, which sits outside the Lyra application boundary.
 *
 * Ports are resolved from environment variables set by global-setup so that
 * parallel test runs can use distinct ports.
 */

import { type Page, expect } from '@playwright/test'

/* ------------------------------------------------------------------ */
/*  Port resolution                                                    */
/* ------------------------------------------------------------------ */

const BACKEND_PORT = Number(process.env.ACCEPTANCE_BACKEND_PORT ?? 8000)
const FRONTEND_PORT = Number(process.env.ACCEPTANCE_FRONTEND_PORT ?? 3000)
const TUTOR_PORT = Number(process.env.ACCEPTANCE_TUTOR_PORT ?? 18_900)

export const BACKEND = `http://127.0.0.1:${BACKEND_PORT}`
export const FRONTEND = `http://127.0.0.1:${FRONTEND_PORT}`
export const TUTOR_CONTROL = `http://127.0.0.1:${TUTOR_PORT}/_control`

const LYRA_HEADERS: Record<string, string> = {
  'Content-Type': 'application/json',
  'X-Lyra-Client': 'acceptance-test',
}

/* ------------------------------------------------------------------ */
/*  Tutor fixture control                                              */
/* ------------------------------------------------------------------ */

export async function setTutorMode(mode: string) {
  const res = await fetch(`${TUTOR_CONTROL}/mode`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }),
  })
  if (!res.ok) throw new Error(`setTutorMode failed: ${res.status}`)
}

export async function enqueueTutorResponse(content: string) {
  const res = await fetch(`${TUTOR_CONTROL}/enqueue`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  })
  if (!res.ok) throw new Error(`enqueueTutorResponse failed: ${res.status}`)
}

export async function getTutorRequests(): Promise<Array<{ url: string; body: unknown }>> {
  const res = await fetch(`${TUTOR_CONTROL}/requests`)
  return res.json()
}

export async function clearTutorState() {
  await fetch(`${TUTOR_CONTROL}/clear`, { method: 'POST' })
}

/**
 * Wait until the tutor fixture has received at least `count` requests.
 * Useful as a deterministic barrier: start a request that will be held by the
 * fixture (timeout/barrier mode), then wait here before attempting a concurrent
 * operation.
 */
export async function waitForTutorRequest(count = 1, timeoutMs = 10_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const reqs = await getTutorRequests()
    if (reqs.length >= count) return
    await sleep(50)
  }
  throw new Error(`Tutor fixture did not receive ${count} request(s) within ${timeoutMs}ms`)
}

/**
 * Wait until the tutor barrier has at least one request waiting, then return.
 * Requires the tutor to be in 'barrier' mode.
 */
export async function waitForBarrier(timeoutMs = 10_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const res = await fetch(`${TUTOR_CONTROL}/barrier/arrived`)
    const { waiting } = await res.json()
    if (waiting > 0) return
    await sleep(50)
  }
  throw new Error('No request arrived at barrier within timeout')
}

/**
 * Release one request held at the tutor barrier, optionally with custom content.
 */
export async function releaseBarrier(content?: string): Promise<void> {
  const res = await fetch(`${TUTOR_CONTROL}/barrier/release`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content: content ?? undefined }),
  })
  if (!res.ok) throw new Error(`releaseBarrier failed: ${res.status}`)
}

/* ------------------------------------------------------------------ */
/*  PLA-291 source-validation barrier (worker-level)                   */
/* ------------------------------------------------------------------ */

export async function enableSourceBarrier(): Promise<void> {
  const res = await fetch(`${BACKEND}/_acceptance/source-barrier/enable`, {
    method: 'POST',
    headers: LYRA_HEADERS,
  })
  if (!res.ok) throw new Error(`enableSourceBarrier failed: ${res.status}`)
}

export async function waitForSourceBarrier(timeoutMs = 15_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const res = await fetch(`${BACKEND}/_acceptance/source-barrier/arrived`)
    const { arrived } = await res.json()
    if (arrived) return
    await sleep(50)
  }
  throw new Error('No worker arrived at source-validation barrier within timeout')
}

export async function releaseSourceBarrier(): Promise<void> {
  const res = await fetch(`${BACKEND}/_acceptance/source-barrier/release`, {
    method: 'POST',
    headers: LYRA_HEADERS,
  })
  if (!res.ok) throw new Error(`releaseSourceBarrier failed: ${res.status}`)
}

/* ------------------------------------------------------------------ */
/*  Backend API helpers                                                */
/* ------------------------------------------------------------------ */

export async function apiPost(path: string, body?: unknown) {
  return fetch(`${BACKEND}${path}`, {
    method: 'POST',
    headers: LYRA_HEADERS,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
}

export async function apiGet(path: string) {
  return fetch(`${BACKEND}${path}`, { headers: LYRA_HEADERS })
}

export async function apiPut(path: string, body: unknown) {
  return fetch(`${BACKEND}${path}`, {
    method: 'PUT',
    headers: LYRA_HEADERS,
    body: JSON.stringify(body),
  })
}

export async function apiPatch(path: string, body: unknown) {
  return fetch(`${BACKEND}${path}`, {
    method: 'PATCH',
    headers: LYRA_HEADERS,
    body: JSON.stringify(body),
  })
}

export async function apiDelete(path: string) {
  return fetch(`${BACKEND}${path}`, {
    method: 'DELETE',
    headers: LYRA_HEADERS,
  })
}

export async function uploadDocument(
  classId: number,
  filePath: string,
  fileName: string,
): Promise<Response> {
  const { readFile } = await import('node:fs/promises')
  const content = await readFile(filePath)
  const form = new FormData()
  form.append('file', new Blob([content]), fileName)
  return fetch(`${BACKEND}/api/classes/${classId}/documents`, {
    method: 'POST',
    headers: { 'X-Lyra-Client': 'acceptance-test' },
    body: form,
  })
}

/* ------------------------------------------------------------------ */
/*  SSE reader                                                         */
/* ------------------------------------------------------------------ */

export interface SSEFrame {
  type: string
  [key: string]: unknown
}

export async function readSSEFrames(res: Response): Promise<SSEFrame[]> {
  const frames: SSEFrame[] = []
  if (!res.body) return frames

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''

      for (const line of lines) {
        const trimmed = line.trim()
        if (!trimmed.startsWith('data:')) continue
        const payload = trimmed.slice(5).trim()
        if (payload === '[DONE]') continue
        try {
          frames.push(JSON.parse(payload))
        } catch {
          // skip non-JSON lines
        }
      }
    }
  } catch {
    // stream may be terminated early
  }
  return frames
}

/* ------------------------------------------------------------------ */
/*  Polling helpers                                                    */
/* ------------------------------------------------------------------ */

export async function waitForDocumentReady(documentId: number, timeoutMs = 30_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const res = await apiGet(`/api/documents/${documentId}/status`)
    const status = await res.json()
    if (status.state === 'ready') return
    if (status.state === 'failed' || status.state === 'unsupported') {
      throw new Error(
        `Document ${documentId} reached terminal state: ${status.state} -- ${status.error_message}`,
      )
    }
    await sleep(300)
  }
  throw new Error(`Document ${documentId} did not become ready within ${timeoutMs}ms`)
}

export async function waitForStudyReady(
  kind: 'decks' | 'quizzes',
  artifactId: number,
  timeoutMs = 30_000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const res = await apiGet(`/api/${kind}/${artifactId}/status`)
    const status = await res.json()
    if (status.state === 'ready') return
    if (status.state === 'failed' || status.state === 'cancelled') {
      throw new Error(
        `Study artifact ${artifactId} reached state: ${status.state} -- ${status.error_message}`,
      )
    }
    await sleep(300)
  }
  throw new Error(`Study artifact ${artifactId} did not become ready within ${timeoutMs}ms`)
}

export async function waitForDraftRun(artifactId: number, timeoutMs = 30_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const res = await apiGet(`/api/drafts/${artifactId}/status`)
    const status = await res.json()
    if (status.state === 'idle' || status.state === 'ready') return
    if (status.state === 'failed') {
      throw new Error(`Draft run failed: ${status.error_message}`)
    }
    await sleep(300)
  }
  throw new Error(`Draft run did not complete within ${timeoutMs}ms`)
}

export async function waitForSolutionSegmented(
  artifactId: number,
  timeoutMs = 60_000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const res = await apiGet(`/api/solutions/${artifactId}/status`)
    const status = await res.json()
    if (status.state === 'awaiting_review') return
    if (status.state === 'ready') return
    if (status.state === 'failed' || status.state === 'cancelled') {
      throw new Error(
        `Solution ${artifactId} reached state: ${status.state} -- ${status.error_message}`,
      )
    }
    await sleep(300)
  }
  throw new Error(`Solution ${artifactId} did not reach awaiting_review within ${timeoutMs}ms`)
}

export async function waitForSolutionReady(artifactId: number, timeoutMs = 60_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const res = await apiGet(`/api/solutions/${artifactId}/status`)
    const status = await res.json()
    if (status.state === 'ready' || status.state === 'idle') return
    if (status.state === 'failed' || status.state === 'cancelled') {
      throw new Error(
        `Solution ${artifactId} reached state: ${status.state} -- ${status.error_message}`,
      )
    }
    await sleep(300)
  }
  throw new Error(`Solution ${artifactId} did not become ready within ${timeoutMs}ms`)
}

/* ------------------------------------------------------------------ */
/*  Seed data helpers                                                  */
/* ------------------------------------------------------------------ */

export async function createClass(name: string): Promise<{ id: number; name: string }> {
  const res = await apiPost('/api/classes', { name })
  if (!res.ok) throw new Error(`createClass failed: ${res.status}`)
  return res.json()
}

export async function createSession(classId: number): Promise<{ id: number }> {
  const res = await apiPost(`/api/classes/${classId}/sessions`, {})
  if (!res.ok) throw new Error(`createSession failed: ${res.status}`)
  return res.json()
}

export async function createDraft(classId: number, title = 'Test Draft'): Promise<{ id: number }> {
  const res = await apiPost(`/api/classes/${classId}/drafts`, { title })
  if (!res.ok) throw new Error(`createDraft failed: ${res.status}`)
  return res.json()
}

/* ------------------------------------------------------------------ */
/*  Page interaction helpers                                           */
/* ------------------------------------------------------------------ */

export async function navigateToClass(page: Page, classId: number) {
  await page.goto(`/classes/${classId}`)
  await page.waitForLoadState('networkidle')
}

export async function navigateToChat(page: Page, classId: number) {
  await page.goto(`/classes/${classId}/chat`)
  await page.waitForLoadState('networkidle')
}

export async function sendChatMessage(page: Page, message: string) {
  const composer = page.locator('#message-composer')
  await composer.fill(message)
  await page.locator('[aria-label="Send message"]').click()
}

export async function waitForChatResponse(page: Page, timeoutMs = 30_000) {
  await expect(page.locator('[aria-label="Send message"]')).toBeVisible({
    timeout: timeoutMs,
  })
}

/**
 * Send a chat message in the browser and wait for the response to complete.
 * Returns the visible assistant message text.
 */
export async function sendChatAndWait(
  page: Page,
  message: string,
  timeoutMs = 30_000,
): Promise<string> {
  await sendChatMessage(page, message)
  await waitForChatResponse(page, timeoutMs)
  // Return the last assistant message
  const msgs = page.locator('[data-role="assistant"]')
  const count = await msgs.count()
  if (count > 0) {
    return (await msgs.nth(count - 1).textContent()) ?? ''
  }
  return ''
}

/* ---- Study browser helpers --------------------------------------- */

/**
 * Click the flashcard to flip it (front -> back or back -> front).
 */
export async function flipCard(page: Page) {
  await page.locator('[role="button"][aria-label*="Card"]').click()
}

/**
 * Rate the current card (requires the card to be flipped to show the back).
 */
export async function rateCard(page: Page, rating: 'Again' | 'Hard' | 'Good' | 'Easy') {
  const group = page.locator('[aria-label="Rate this card"]')
  await group.getByRole('button', { name: rating }).click()
}

/**
 * Click "Study again" from the session summary screen.
 */
export async function clickStudyAgain(page: Page) {
  const summary = page.locator('[aria-label="Session summary"]')
  await summary.getByRole('button', { name: 'Study again' }).click()
}

/**
 * Wait for the session summary to appear (all cards reviewed).
 */
export async function waitForSessionSummary(page: Page, timeoutMs = 15_000) {
  await expect(page.locator('[aria-label="Session summary"]')).toBeVisible({
    timeout: timeoutMs,
  })
}

/**
 * Get the current card position text (e.g. "Card 1 of 4").
 */
export async function getCardPosition(page: Page): Promise<string> {
  return (await page.getByText(/Card \d+ of \d+/).textContent()) ?? ''
}

/* ---- Quiz browser helpers ---------------------------------------- */

/**
 * Select an MCQ answer option by index (0-based).
 * MCQ options are plain buttons inside a list -- clicking auto-submits.
 */
export async function answerQuizQuestion(page: Page, optionIndex: number) {
  const questionList = page.locator('main ul > li button')
  await questionList.nth(optionIndex).click()
}

/**
 * Click "Next" or "See results" after answering.
 */
export async function advanceQuiz(page: Page) {
  const nextBtn = page.getByRole('button', { name: /Next|See results/ })
  await expect(nextBtn).toBeVisible({ timeout: 5_000 })
  await nextBtn.click()
}

/**
 * Wait for quiz results to appear.
 */
export async function waitForQuizResults(page: Page, timeoutMs = 15_000) {
  await expect(page.locator('[aria-label="Quiz results"]')).toBeVisible({
    timeout: timeoutMs,
  })
}

/* ------------------------------------------------------------------ */
/*  Origin security helpers                                            */
/* ------------------------------------------------------------------ */

export async function fetchWithOrigin(
  path: string,
  origin: string,
  method = 'POST',
  body?: unknown,
) {
  return fetch(`${BACKEND}${path}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      'Origin': origin,
      'Host': `127.0.0.1:${BACKEND_PORT}`,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
}

/* ------------------------------------------------------------------ */
/*  Harness lifecycle helpers                                          */
/* ------------------------------------------------------------------ */

/**
 * Read the acceptance state file to get the data directory, backend PID,
 * and port configuration. Returns null if the file cannot be read.
 */
export async function readAcceptanceState(): Promise<{
  runId: string
  dataDir: string
  backendPid: number
  frontendPid: number
  backendPort: number
  frontendPort: number
  tutorPort: number
} | null> {
  const { readdir, readFile } = await import('node:fs/promises')
  const { resolve, join } = await import('node:path')
  const projectRoot = resolve(__dirname, '..', '..', '..')

  // Find the state file (unique per run)
  const entries = await readdir(projectRoot)
  const stateFiles = entries.filter(
    (e) => e.startsWith('.acceptance-state-') && e.endsWith('.json'),
  )
  if (stateFiles.length === 0) return null

  const raw = await readFile(join(projectRoot, stateFiles[0]), 'utf-8')
  return JSON.parse(raw)
}

/**
 * Stop the backend process, wait for it to exit, then spawn a fresh one
 * on the same port with the same data directory. Returns the new ChildProcess.
 * Used by restart-reconciliation tests.
 */
export async function restartBackend(): Promise<void> {
  const { spawn } = await import('node:child_process')
  const { readdir, readFile, writeFile } = await import('node:fs/promises')
  const { resolve, join } = await import('node:path')
  const projectRoot = resolve(__dirname, '..', '..', '..')

  const state = await readAcceptanceState()
  if (!state) throw new Error('Cannot restart backend: no state file found')

  // Kill the current backend: SIGTERM → wait → SIGKILL → wait → prove dead
  try {
    process.kill(state.backendPid, 'SIGTERM')
  } catch {
    /* already gone */
  }

  const deadline = Date.now() + 5_000
  while (Date.now() < deadline) {
    try {
      process.kill(state.backendPid, 0)
      await sleep(100)
    } catch {
      break
    }
  }

  // SIGKILL fallback if still alive
  try {
    process.kill(state.backendPid, 0)
    process.kill(state.backendPid, 'SIGKILL')
    const killDeadline = Date.now() + 3_000
    while (Date.now() < killDeadline) {
      try {
        process.kill(state.backendPid, 0)
        await sleep(100)
      } catch {
        break
      }
    }
  } catch {
    /* already gone */
  }

  // Wait for port to be free
  const portDeadline = Date.now() + 5_000
  while (Date.now() < portDeadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${state.backendPort}/api/health/live`, {
        signal: AbortSignal.timeout(500),
      })
      void res.body?.cancel()
      await sleep(200)
    } catch {
      break
    }
  }

  // Spawn a new backend on the same port with the same data dir
  const backendEnv: Record<string, string> = {}
  for (const [k, v] of Object.entries(process.env)) {
    if (v !== undefined) backendEnv[k] = v
  }
  backendEnv.LYRA_DATA_DIR = state.dataDir
  backendEnv.LYRA_HOST = '127.0.0.1'
  backendEnv.LYRA_PORT = String(state.backendPort)
  backendEnv.PYTHONDONTWRITEBYTECODE = '1'

  const newBackend = spawn(
    'uv',
    [
      'run',
      'python',
      '-m',
      'uvicorn',
      'acceptance.backend_harness:app',
      '--host',
      '127.0.0.1',
      '--port',
      String(state.backendPort),
      '--log-level',
      'warning',
    ],
    {
      cwd: projectRoot,
      env: backendEnv as NodeJS.ProcessEnv,
      stdio: ['ignore', 'pipe', 'pipe'],
      detached: false,
    },
  )

  // Wait for the new backend to be ready
  const readyDeadline = Date.now() + 60_000
  while (Date.now() < readyDeadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${state.backendPort}/api/health/ready`, {
        signal: AbortSignal.timeout(2000),
      })
      if (res.ok) break
    } catch {
      // not ready yet
    }
    await sleep(500)
  }

  // Update the state file and globalThis with the new PID
  const entries = await readdir(projectRoot)
  const stateFiles = entries.filter(
    (e) => e.startsWith('.acceptance-state-') && e.endsWith('.json'),
  )
  if (stateFiles.length > 0) {
    const filePath = join(projectRoot, stateFiles[0])
    const raw = await readFile(filePath, 'utf-8')
    const stateObj = JSON.parse(raw)
    stateObj.backendPid = newBackend.pid
    stateObj.backendStartToken = null
    await writeFile(filePath, JSON.stringify(stateObj))
  }

  // Update in-memory state if available
  const mem = (globalThis as Record<string, unknown>).__acceptanceState as
    { backend: import('node:child_process').ChildProcess } | undefined
  if (mem) {
    mem.backend = newBackend
  }
}

/* ------------------------------------------------------------------ */
/*  Utilities                                                          */
/* ------------------------------------------------------------------ */

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}
