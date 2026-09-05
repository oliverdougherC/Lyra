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

export const LYRA_HEADERS: Record<string, string> = {
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

export async function enqueueTutorResponse(
  item: string | { content?: string; raw?: Record<string, unknown> },
) {
  const body = typeof item === 'string' ? { content: item } : item
  const res = await fetch(`${TUTOR_CONTROL}/enqueue`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
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

export async function enableToolBarrier(): Promise<void> {
  const res = await fetch(`${BACKEND}/_acceptance/tool-barrier/enable`, {
    method: 'POST',
    headers: LYRA_HEADERS,
  })
  if (!res.ok) throw new Error(`enableToolBarrier failed: ${res.status}`)
}

export async function waitForToolBarrier(timeoutMs = 15_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const res = await fetch(`${BACKEND}/_acceptance/tool-barrier/arrived`)
    const { arrived } = await res.json()
    if (arrived) return
    await sleep(50)
  }
  throw new Error('No dispatch worker arrived at the tool barrier within timeout')
}

export async function releaseToolBarrier(): Promise<void> {
  const res = await fetch(`${BACKEND}/_acceptance/tool-barrier/release`, {
    method: 'POST',
    headers: LYRA_HEADERS,
  })
  if (!res.ok) throw new Error(`releaseToolBarrier failed: ${res.status}`)
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
  await page.getByRole('button', { name: /^Show (answer|question)$/ }).click()
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
  const { execSync, spawn } = await import('node:child_process')
  const { readdir, readFile, writeFile } = await import('node:fs/promises')
  const { resolve, join } = await import('node:path')
  const projectRoot = resolve(__dirname, '..', '..', '..')

  const state = await readAcceptanceState()
  if (!state) throw new Error('Cannot restart backend: no state file found')

  // Kill the current backend. It was spawned detached (its own process group, pgid == pid), so
  // signal the WHOLE group: that reclaims the real uvicorn python grandchild and its fake-helper
  // great-grandchild. Signalling only the `uv` wrapper pid orphans them -- the old helper keeps
  // holding port 19500 and the new backend cannot bring up its own. This is the same orphan-leak
  // class the teardown process-group fix addresses, applied to the in-run restart path.
  const killGroup = (sig: NodeJS.Signals): void => {
    try {
      process.kill(-state.backendPid, sig)
    } catch {
      try {
        process.kill(state.backendPid, sig)
      } catch {
        /* already gone */
      }
    }
  }

  killGroup('SIGTERM')
  const deadline = Date.now() + 5_000
  while (Date.now() < deadline) {
    let alive = true
    try {
      process.kill(state.backendPid, 0)
      alive = true
    } catch {
      alive = false
    }
    if (!alive) break
    await sleep(100)
  }

  // SIGKILL fallback if still alive
  if (isAlive(state.backendPid)) {
    killGroup('SIGKILL')
    const killDeadline = Date.now() + 3_000
    while (Date.now() < killDeadline && isAlive(state.backendPid)) {
      await sleep(100)
    }
  }

  // A SIGKILLed uvicorn never runs its atexit helper-reclaim, so the old fake-helper may still hold
  // the helper port (19500, fixed by the harness). Sweep any acceptance fake-helper by its unique
  // command-line signature (it can never match a production Lyra process) and wait for that port to
  // be free before spawning fresh -- otherwise the new backend's helper cannot bind.
  await sweepFakeHelpers()
  await waitForPortFree(19_500, 10_000)

  // Wait for the backend port to be free
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

  // Spawn a new backend on the same port with the same data dir. Detached so it is its own process
  // group leader: this run's teardown (and any later restart) can reclaim it and its helper tree by
  // signalling -pid rather than orphaning the grandchild processes.
  const backendEnv: Record<string, string> = {}
  for (const [k, v] of Object.entries(process.env)) {
    if (v !== undefined) backendEnv[k] = v
  }
  backendEnv.LYRA_DATA_DIR = state.dataDir
  backendEnv.LYRA_HOST = '127.0.0.1'
  backendEnv.LYRA_PORT = String(state.backendPort)
  backendEnv.LYRA_BROWSER_ORIGINS = `http://127.0.0.1:${FRONTEND_PORT}`
  backendEnv.PYTHONDONTWRITEBYTECODE = '1'

  // stdio is fully ignored: this worker process exits when its tests finish, and a piped
  // stdout/stderr that nobody drains would fill its 64KB buffer and block the backend
  // mid-suite (or EPIPE it when the worker exits). The replacement's logs are lost, but
  // the backend-failure ledger still records anything that matters.
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
      stdio: ['ignore', 'ignore', 'ignore'],
      detached: true,
    },
  )
  if (!newBackend.pid) throw new Error('restartBackend: replacement backend did not spawn')

  // Wait for the new backend to be ready (its helper is up and the app reports ready).
  let becameReady = false
  const readyDeadline = Date.now() + 60_000
  while (Date.now() < readyDeadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${state.backendPort}/api/health/ready`, {
        signal: AbortSignal.timeout(2000),
      })
      if (res.ok) {
        becameReady = true
        break
      }
    } catch {
      // not ready yet
    }
    await sleep(500)
  }
  if (!becameReady) {
    throw new Error('restartBackend: replacement backend never became ready')
  }

  // Persist the replacement's identity. The state FILE is authoritative for the current
  // backend lifetime: this function runs in a Playwright WORKER process, so global
  // teardown's in-memory ChildProcess (held by the setup/teardown process) can never be
  // updated from here -- teardown must read this file and, on a PID change, own the
  // replacement instead of the stale original. The birth token is REAL (same `ps lstart`
  // format setup/teardown use) so teardown keeps PID-reuse safety across the handoff.
  // Detached spawn makes the replacement its own process-group leader (pgid == pid).
  const newToken = (() => {
    try {
      return execSync(`ps -p ${newBackend.pid} -o lstart=`, {
        encoding: 'utf-8',
        timeout: 2000,
      }).trim()
    } catch {
      return null
    }
  })()
  if (!newToken) {
    throw new Error(
      `restartBackend: could not read birth token for replacement backend pid ${newBackend.pid}`,
    )
  }

  const entries = await readdir(projectRoot)
  const stateFiles = entries.filter(
    (e) => e.startsWith('.acceptance-state-') && e.endsWith('.json'),
  )
  if (stateFiles.length === 0) {
    throw new Error('restartBackend: acceptance state file disappeared; cannot record ownership')
  }
  const filePath = join(projectRoot, stateFiles[0])
  const raw = await readFile(filePath, 'utf-8')
  const stateObj = JSON.parse(raw)
  stateObj.backendPid = newBackend.pid
  stateObj.backendStartToken = newToken
  await writeFile(filePath, JSON.stringify(stateObj))

  // Let the worker exit without waiting on the detached replacement.
  newBackend.unref()
}

function isAlive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

/** Kill any lingering acceptance fake-helper by its unique command-line signature. */
async function sweepFakeHelpers(): Promise<void> {
  const { execSync } = await import('node:child_process')
  let listing: string
  try {
    listing = execSync('ps -axww -o pid=,command=', { encoding: 'utf-8', timeout: 5000 })
  } catch {
    return
  }
  const pids: number[] = []
  for (const line of listing.split('\n')) {
    if (!line.includes('e2e/acceptance/fake-helper.py')) continue
    const field = line.trim().split(/\s+/)[0]
    if (field && Number(field) !== process.pid) pids.push(Number(field))
  }
  for (const pid of pids) {
    try {
      process.kill(pid, 'SIGKILL')
    } catch {
      /* already gone */
    }
  }
}

/** Wait until nothing is listening on the given port (a free bind attempt succeeds). */
async function waitForPortFree(port: number, timeoutMs: number): Promise<void> {
  const { createServer } = await import('node:net')
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const free = await new Promise<boolean>((resolve) => {
      const srv = createServer()
      srv.once('error', () => resolve(false))
      srv.listen(port, '127.0.0.1', () => {
        srv.close(() => resolve(true))
      })
    })
    if (free || Date.now() >= deadline) return
    await sleep(150)
  }
}

/**
 * Kill whatever process is LISTENING on the given port, by PID. Used as a deterministic
 * safety net after a process-group kill to guarantee the port is freed (the zero-orphan gate
 * forbids a leftover fixture holding a port, which would poison the next run). Targets only
 * the listener on that specific port -- it can never match an unrelated process.
 */
export async function killPortListeners(port: number): Promise<void> {
  const { execSync } = await import('node:child_process')
  let pids: string[] = []
  try {
    pids = (
      execSync(`lsof -tiTCP:${port} -sTCP:LISTEN`, { encoding: 'utf-8', timeout: 5000 }) || ''
    )
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean)
  } catch {
    return // nothing listening (or lsof unavailable)
  }
  for (const pidStr of pids) {
    const pid = Number(pidStr)
    if (!Number.isFinite(pid) || pid === process.pid) continue
    try {
      process.kill(pid, 'SIGKILL')
    } catch {
      /* already gone */
    }
  }
  // Give the kernel a moment to release the socket.
  await sleep(150)
}

/* ------------------------------------------------------------------ */
/*  Utilities                                                          */
/* ------------------------------------------------------------------ */

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}
