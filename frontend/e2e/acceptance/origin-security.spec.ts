/**
 * Origin security through the real running backend (PLA-304).
 *
 * Proves: hostile browser Origin requests cannot mutate local state, while
 * legitimate Lyra-origin requests still work.  Hits representative JSON,
 * streaming, multipart, and empty-body mutations through the actual running
 * backend.  Includes a real browser-driven test where Playwright loads an
 * attacker page on a distinct origin and attempts a cross-origin mutation.
 */

import { test, expect } from '@playwright/test'
import { createServer, type Server } from 'node:http'
import { resolve } from 'node:path'
import { createClass, fetchWithOrigin, apiPost, BACKEND } from './helpers'

const TEST_DATA = resolve(__dirname, 'test-data')
const EVIL_ORIGIN = 'http://evil.example.com'
const GOOD_ORIGIN = `http://127.0.0.1:${process.env.ACCEPTANCE_FRONTEND_PORT ?? 3000}`

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
        'Host': `127.0.0.1:${process.env.ACCEPTANCE_BACKEND_PORT ?? 8000}`,
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
        Host: `127.0.0.1:${process.env.ACCEPTANCE_BACKEND_PORT ?? 8000}`,
      },
    })
    expect(res.ok).toBe(true)
  })

  test('streaming chat endpoint blocked from evil origin', async () => {
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
        Host: `127.0.0.1:${process.env.ACCEPTANCE_BACKEND_PORT ?? 8000}`,
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
        Host: `127.0.0.1:${process.env.ACCEPTANCE_BACKEND_PORT ?? 8000}`,
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
    const http = await import('node:http')
    const backendPort = Number(process.env.ACCEPTANCE_BACKEND_PORT ?? 8000)
    const status = await new Promise<number>((resolve, reject) => {
      const req = http.request(
        {
          hostname: '127.0.0.1',
          port: backendPort,
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

  test('browser: real attacker page on distinct origin cannot POST to backend', async ({
    page,
  }) => {
    const backendPort = Number(process.env.ACCEPTANCE_BACKEND_PORT ?? 8000)

    // Serve an attacker page on a different port (distinct origin)
    const attackerPort = backendPort + 100
    const attackerPage = `
<!DOCTYPE html>
<html>
<body>
<script>
async function attack() {
  try {
    const res = await fetch('http://127.0.0.1:${backendPort}/api/classes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'Browser Attack Class' }),
      mode: 'cors',
    });
    window.__attackResult = { status: res.status, ok: res.ok };
  } catch (e) {
    // CORS preflight failure or network error
    window.__attackResult = { status: 0, error: e.message, blocked: true };
  }
}
attack();
</script>
</body>
</html>`

    let attackerServer: Server | null = null
    try {
      attackerServer = createServer((_req, res) => {
        res.writeHead(200, { 'Content-Type': 'text/html' })
        res.end(attackerPage)
      })
      await new Promise<void>((resolve) => {
        attackerServer!.listen(attackerPort, '127.0.0.1', () => resolve())
      })

      // Navigate the Playwright browser to the attacker page
      await page.goto(`http://127.0.0.1:${attackerPort}`)

      // Wait for the attack to complete
      await page.waitForFunction(() => (window as any).__attackResult !== undefined, null, {
        timeout: 10_000,
      })

      const result = await page.evaluate(() => (window as any).__attackResult)

      // The attack should be blocked: either by CORS (status 0, network error)
      // or by the backend's origin check (status 403)
      if (result.blocked) {
        expect(result.status).toBe(0) // CORS preflight blocked
      } else {
        expect(result.status).toBe(403) // Backend origin check blocked
      }
    } finally {
      if (attackerServer) {
        await new Promise<void>((resolve) => attackerServer!.close(() => resolve()))
      }
    }
  })
})
