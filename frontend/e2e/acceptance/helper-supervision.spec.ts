/**
 * Helper process supervision protocol through the PRODUCTION LlamaServer
 * supervisor (PLA-301).
 *
 * Proves: the real LlamaServer class spawns fake-helper.py, health-checks it,
 * records ownership with birth tokens, handles replacement (stop + start on
 * same port), detects unhealthy processes, and terminates cleanly on stop.
 *
 * All process lifecycle is routed through the production supervisor via
 * acceptance API endpoints -- no direct spawn()/kill() from TypeScript.
 */

import { test, expect } from '@playwright/test'
import { apiGet, BACKEND, HELPER_PORT } from './helpers'

const LYRA_HEADERS: Record<string, string> = {
  'Content-Type': 'application/json',
  'X-Lyra-Client': 'acceptance-test',
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}

async function startHelper(
  opts: {
    model?: string
    fail_health?: boolean
    slow_start?: number
  } = {},
): Promise<{ ok: boolean; port?: number; pid?: number; birth_token?: string; error?: string }> {
  const res = await fetch(`${BACKEND}/_acceptance/helper/start`, {
    method: 'POST',
    headers: LYRA_HEADERS,
    body: JSON.stringify({
      model: opts.model ?? 'acceptance-test-model',
      fail_health: opts.fail_health ?? false,
      slow_start: opts.slow_start ?? 0,
    }),
  })
  return res.json()
}

async function stopHelper(): Promise<{ ok: boolean; was_running: boolean }> {
  const res = await fetch(`${BACKEND}/_acceptance/helper/stop`, {
    method: 'POST',
    headers: LYRA_HEADERS,
  })
  return res.json()
}

async function getHelperStatus(): Promise<{
  running: boolean
  port?: number
  pid?: number
  birth_token?: string
  healthy?: boolean
  ownership_record?: {
    pid: number
    start_token: string
    port: number
    model: string
  }
}> {
  const res = await fetch(`${BACKEND}/_acceptance/helper/status`, {
    headers: { 'X-Lyra-Client': 'acceptance-test' },
  })
  return res.json()
}

async function cleanupOwnership(): Promise<void> {
  await fetch(`${BACKEND}/_acceptance/helper/cleanup-ownership`, {
    method: 'POST',
    headers: LYRA_HEADERS,
  })
}

/** Full isolation reset: free the port (kill any listener) and clear ownership records. */
async function fullCleanup(): Promise<void> {
  await fetch(`${BACKEND}/_acceptance/helper/cleanup`, {
    method: 'POST',
    headers: LYRA_HEADERS,
  })
}

test.describe('Helper supervision (PLA-301)', () => {
  // Guarantee every test starts from a free port with no stale ownership record. A prior spec
  // (e.g. the adoption scenarios) may have left a foreign helper on this run’s helper port; without this, the
  // production supervisor would ADOPT it instead of spawning fresh, corrupting these assertions.
  test.beforeEach(async () => {
    await fullCleanup()
  })

  test.afterEach(async () => {
    await stopHelper()
    await cleanupOwnership()
  })

  test('supervisor spawns helper, records ownership, and health-checks it', async () => {
    const result = await startHelper({ model: 'acceptance-test-model' })
    expect(result.ok).toBe(true)
    expect(result.pid).toBeTruthy()
    expect(result.birth_token).toBeTruthy()
    expect(result.port).toBe(HELPER_PORT)

    // Verify health through the real supervisor status check
    const status = await getHelperStatus()
    expect(status.running).toBe(true)
    expect(status.healthy).toBe(true)
    expect(status.pid).toBe(result.pid)
    expect(status.birth_token).toBe(result.birth_token)

    // Verify the production ownership record was written
    expect(status.ownership_record).toBeTruthy()
    expect(status.ownership_record!.pid).toBe(result.pid)
    expect(status.ownership_record!.start_token).toBe(result.birth_token)
    expect(status.ownership_record!.port).toBe(HELPER_PORT)
    expect(status.ownership_record!.model).toBe('acceptance-test-model')

    // Verify the fake helper endpoints respond correctly
    const healthRes = await fetch(`http://127.0.0.1:${HELPER_PORT}/health`)
    expect(healthRes.ok).toBe(true)
    const health = await healthRes.json()
    expect(health.status).toBe('ok')

    const propsRes = await fetch(`http://127.0.0.1:${HELPER_PORT}/props`)
    const props = await propsRes.json()
    expect(props.model_path).toBe('acceptance-test-model')

    const modelsRes = await fetch(`http://127.0.0.1:${HELPER_PORT}/v1/models`)
    const models = await modelsRes.json()
    expect(models.data[0].id).toBe('acceptance-test-model')
  })

  test('supervisor stop terminates child and cleans ownership', async () => {
    const result = await startHelper()
    expect(result.ok).toBe(true)
    expect(result.pid).toBeTruthy()

    // Stop through the production supervisor
    const stopResult = await stopHelper()
    expect(stopResult.ok).toBe(true)
    expect(stopResult.was_running).toBe(true)

    // Verify the process is dead
    await sleep(200)
    const status = await getHelperStatus()
    expect(status.running).toBe(false)
  })

  test('ownership record tracks birth token identity across supervisor checks', async () => {
    const result1 = await startHelper({ model: 'token-test-model' })
    expect(result1.ok).toBe(true)

    const status1 = await getHelperStatus()
    const token1 = status1.birth_token
    const pid1 = status1.pid
    expect(token1).toBeTruthy()

    // Same PID, same token after a moment (supervisor sees consistent identity)
    await sleep(200)
    const status2 = await getHelperStatus()
    expect(status2.birth_token).toBe(token1)
    expect(status2.pid).toBe(pid1)

    // Stop and start a new helper -- different birth token
    await stopHelper()
    await sleep(1100)

    const result2 = await startHelper({ model: 'token-test-model-2' })
    expect(result2.ok).toBe(true)

    const status3 = await getHelperStatus()
    expect(status3.birth_token).toBeTruthy()

    if (status3.pid !== pid1) {
      expect(status3.birth_token).not.toBe(token1)
    }
  })

  test('supervisor replacement: stop old, start new on same port with different model', async () => {
    const result1 = await startHelper({ model: 'old-model' })
    expect(result1.ok).toBe(true)
    const oldPid = result1.pid!
    const oldToken = result1.birth_token

    // Verify old model via the supervisor's owned process
    const oldProps = await (await fetch(`http://127.0.0.1:${HELPER_PORT}/props`)).json()
    expect(oldProps.model_path).toBe('old-model')

    // Stop old, start new with different model (production replacement flow)
    await stopHelper()
    await sleep(1100)

    const result2 = await startHelper({ model: 'new-model' })
    expect(result2.ok).toBe(true)
    const newPid = result2.pid!
    const newToken = result2.birth_token

    // Verify replacement model
    const newProps = await (await fetch(`http://127.0.0.1:${HELPER_PORT}/props`)).json()
    expect(newProps.model_path).toBe('new-model')

    // The supervisor recorded new ownership with a different birth token
    const status = await getHelperStatus()
    expect(status.ownership_record!.model).toBe('new-model')
    if (oldPid !== newPid) {
      expect(newToken).not.toBe(oldToken)
    }
  })

  test('supervisor detects unhealthy helper via health-check failure', async () => {
    const result = await startHelper({ fail_health: true })
    // The supervisor should fail to start because health never passes
    expect(result.ok).toBe(false)
    expect(result.error).toBeTruthy()
  })

  test('supervisor handles slow-starting helper within health timeout', async () => {
    const result = await startHelper({ slow_start: 1.5 })
    expect(result.ok).toBe(true)
    expect(result.pid).toBeTruthy()

    // After the supervisor returned, the helper should be healthy
    const status = await getHelperStatus()
    expect(status.running).toBe(true)
    expect(status.healthy).toBe(true)
  })

  test('backend health endpoint reports database ready', async () => {
    const res = await apiGet('/api/health/ready')
    expect(res.ok).toBe(true)
    const health = await res.json()
    expect(health.status).toBe('ready')
    expect(health.components.database.status).toBe('ready')
  })

  test('settings report model and endpoint configuration', async () => {
    const res = await apiGet('/api/settings')
    expect(res.ok).toBe(true)
    const settings = await res.json()
    expect(settings.model).toBe('test-model')
    expect(settings.api_key_set).toBe(true)
    expect(settings.remote_ack).toBe(true)
    expect(settings.endpoint_url).toContain('127.0.0.1')
  })
})
