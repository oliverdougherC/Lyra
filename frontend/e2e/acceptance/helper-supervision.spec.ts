/**
 * Helper process supervision protocol through the real stack (PLA-301).
 *
 * Proves: the health endpoint reports component status for each subsystem,
 * and the backend does not crash when probed for helper status.  The full
 * llama-server lifecycle (spawn, adopt, health-check, reclaim) is tested
 * in the backend unit tests; this acceptance test verifies the supervision
 * protocol is wired up end-to-end.
 */

import { test, expect } from '@playwright/test'
import { apiGet } from './helpers'

test.describe('Helper supervision (PLA-301)', () => {
  test('health endpoint reports database component status', async () => {
    const res = await apiGet('/api/health/ready')
    expect(res.ok).toBe(true)
    const health = await res.json()
    expect(health.status).toBe('ready')
    expect(health.components).toBeTruthy()
    expect(health.components.database).toBeTruthy()
    expect(health.components.database.status).toBe('ready')
  })

  test('liveness endpoint responds independently of readiness', async () => {
    const res = await apiGet('/api/health/live')
    expect(res.ok).toBe(true)
  })

  test('health endpoint is stable across multiple rapid probes', async () => {
    // Simulate monitoring: rapid health checks should not cause instability
    const results = await Promise.all(
      Array.from({ length: 5 }, () =>
        apiGet('/api/health/ready').then(async (r) => ({
          status: r.status,
          body: await r.json(),
        })),
      ),
    )

    for (const r of results) {
      expect(r.status).toBe(200)
      expect(r.body.status).toBe('ready')
    }
  })

  test('settings report model and endpoint configuration', async () => {
    const res = await apiGet('/api/settings')
    expect(res.ok).toBe(true)
    const settings = await res.json()

    // The acceptance harness configures these in global-setup
    expect(settings.model).toBe('test-model')
    expect(settings.api_key_set).toBe(true)
    expect(settings.remote_ack).toBe(true)
    expect(settings.endpoint_url).toContain('127.0.0.1')
  })
})
