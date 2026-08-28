/**
 * Shared helpers for acceptance specs.
 *
 * Every helper communicates with the running stack over HTTP — no mocks, no
 * monkeypatches, no intercepted routes.  The only "control" API is the tutor
 * fixture, which sits outside the Lyra application boundary.
 */

import { type Page, expect } from '@playwright/test'

export const BACKEND = 'http://127.0.0.1:8000'
export const TUTOR_CONTROL = 'http://127.0.0.1:18900/_control'

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

/* ------------------------------------------------------------------ */
/*  Backend API helpers                                                */
/* ------------------------------------------------------------------ */

export async function apiPost(path: string, body?: unknown) {
  const res = await fetch(`${BACKEND}${path}`, {
    method: 'POST',
    headers: LYRA_HEADERS,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  return res
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
        `Document ${documentId} reached terminal state: ${status.state} — ${status.error_message}`,
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
        `Study artifact ${artifactId} reached state: ${status.state} — ${status.error_message}`,
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
  // Wait for the streaming to complete — the send button reappears
  await expect(page.locator('[aria-label="Send message"]')).toBeVisible({
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
      'Host': '127.0.0.1:8000',
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
}

/* ------------------------------------------------------------------ */
/*  Utilities                                                          */
/* ------------------------------------------------------------------ */

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}
