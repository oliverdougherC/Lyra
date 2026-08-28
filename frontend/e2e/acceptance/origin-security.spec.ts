/**
 * Origin security through the real running backend (PLA-304).
 *
 * Proves: hostile browser Origin requests cannot mutate local state, while
 * legitimate Lyra-origin requests still work.  Hits representative JSON,
 * streaming, multipart, and empty-body mutations through the actual running
 * backend rather than only TestClient middleware tests.
 */

import { test, expect } from '@playwright/test'
import { resolve } from 'node:path'
import { createClass, fetchWithOrigin, apiPost, BACKEND } from './helpers'

const TEST_DATA = resolve(__dirname, 'test-data')
const EVIL_ORIGIN = 'http://evil.example.com'
const GOOD_ORIGIN = 'http://127.0.0.1:3000'

test.describe('Origin security (PLA-304)', () => {
  let classId: number

  test.beforeAll(async () => {
    const cls = await createClass('Acceptance: Origin Security')
    classId = cls.id
  })

  test('JSON mutation blocked from evil origin', async () => {
    const res = await fetchWithOrigin('/api/classes', EVIL_ORIGIN, 'POST', { name: 'Evil Class' })
    expect(res.status).toBe(403)
    const body = await res.json()
    expect(body.detail).toMatch(/origin|trusted/i)
  })

  test('JSON mutation works from legitimate origin', async () => {
    const res = await fetchWithOrigin('/api/classes', GOOD_ORIGIN, 'POST', { name: 'Good Class' })
    expect(res.status).toBe(201)
  })

  test('mutation works with X-Lyra-Client header (no origin)', async () => {
    const res = await fetch(`${BACKEND}/api/classes`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Lyra-Client': 'test-cli',
        'Host': '127.0.0.1:8000',
      },
      body: JSON.stringify({ name: 'CLI Class' }),
    })
    expect(res.status).toBe(201)
  })

  test('DELETE mutation blocked from evil origin', async () => {
    const res = await fetchWithOrigin(`/api/classes/${classId}`, EVIL_ORIGIN, 'DELETE')
    expect(res.status).toBe(403)
  })

  test('PATCH mutation blocked from evil origin', async () => {
    const res = await fetchWithOrigin(`/api/classes/${classId}`, EVIL_ORIGIN, 'PATCH', {
      name: 'Evil Rename',
    })
    expect(res.status).toBe(403)
  })

  test('PUT settings blocked from evil origin', async () => {
    const res = await fetchWithOrigin('/api/settings', EVIL_ORIGIN, 'PUT', { model: 'evil-model' })
    expect(res.status).toBe(403)
  })

  test('GET requests pass regardless of origin (safe method)', async () => {
    const res = await fetch(`${BACKEND}/api/classes`, {
      headers: {
        Origin: EVIL_ORIGIN,
        Host: '127.0.0.1:8000',
      },
    })
    expect(res.ok).toBe(true)
  })

  test('streaming chat endpoint blocked from evil origin', async () => {
    // Create a session first (via legitimate request)
    const session = await apiPost(`/api/classes/${classId}/sessions`, {})
    const sessionData = await session.json()

    const res = await fetchWithOrigin(`/api/sessions/${sessionData.id}/chat`, EVIL_ORIGIN, 'POST', {
      content: 'Evil message',
      mode: 'guide',
    })
    expect(res.status).toBe(403)
  })

  test('multipart upload blocked from evil origin', async () => {
    const { readFile } = await import('node:fs/promises')
    const content = await readFile(resolve(TEST_DATA, 'sample.txt'))
    const form = new FormData()
    form.append('file', new Blob([content]), 'evil-upload.txt')

    const res = await fetch(`${BACKEND}/api/classes/${classId}/documents`, {
      method: 'POST',
      headers: {
        Origin: EVIL_ORIGIN,
        Host: '127.0.0.1:8000',
      },
      body: form,
    })
    expect(res.status).toBe(403)
  })

  test('multipart upload succeeds from legitimate origin', async () => {
    const { readFile } = await import('node:fs/promises')
    const content = await readFile(resolve(TEST_DATA, 'sample.txt'))
    const form = new FormData()
    form.append('file', new Blob([content]), 'good-upload.txt')

    const res = await fetch(`${BACKEND}/api/classes/${classId}/documents`, {
      method: 'POST',
      headers: {
        Origin: GOOD_ORIGIN,
        Host: '127.0.0.1:8000',
      },
      body: form,
    })
    expect(res.status).toBe(202)
  })

  test('localhost:3000 origin also accepted', async () => {
    const res = await fetchWithOrigin('/api/classes', 'http://localhost:3000', 'POST', {
      name: 'Localhost Class',
    })
    expect(res.status).toBe(201)
  })

  test('DNS rebinding via Host header rejected', async () => {
    // Node's fetch() strips custom Host headers (it's a forbidden header).
    // Use node:http directly so the spoofed Host actually reaches the server.
    const http = await import('node:http')
    const status = await new Promise<number>((resolve, reject) => {
      const req = http.request(
        {
          hostname: '127.0.0.1',
          port: 8000,
          path: '/api/health/live',
          method: 'GET',
          headers: { Host: 'attacker.example.com' },
        },
        (res) => resolve(res.statusCode ?? 0),
      )
      req.on('error', reject)
      req.end()
    })
    expect(status).toBe(400)
  })
})
