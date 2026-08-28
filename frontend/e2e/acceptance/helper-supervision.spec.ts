/**
 * Helper process supervision protocol through real subprocesses (PLA-301).
 *
 * Proves: spawn a real child process, health-check it, send SIGTERM and
 * verify clean exit, verify birth token identity survives across checks,
 * and detect health failure.
 *
 * Uses a tiny Python fake helper (fake-helper.py) that speaks the same
 * /health and /props HTTP interface as llama-server.
 */

import { test, expect } from '@playwright/test'
import { type ChildProcess, execSync, spawn } from 'node:child_process'
import { resolve } from 'node:path'
import { apiGet } from './helpers'

const PROJECT_ROOT = resolve(__dirname, '..', '..', '..')
const FAKE_HELPER = resolve(__dirname, 'fake-helper.py')
const HELPER_PORT = 19_500

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}

async function waitForHelper(port: number, timeoutMs = 10_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/health`, {
        signal: AbortSignal.timeout(1000),
      })
      if (res.ok) return
    } catch {
      // not ready
    }
    await sleep(100)
  }
  throw new Error(`Helper on port ${port} did not become ready within ${timeoutMs}ms`)
}

function spawnHelper(
  port: number,
  opts: { model?: string; hangHealth?: boolean; failHealth?: boolean; slowStart?: number } = {},
): ChildProcess {
  const args = ['run', 'python', FAKE_HELPER, '--port', String(port)]
  if (opts.model) args.push('--model', opts.model)
  if (opts.hangHealth) args.push('--hang-health')
  if (opts.failHealth) args.push('--fail-health')
  if (opts.slowStart) args.push('--slow-start', String(opts.slowStart))
  return spawn('uv', args, {
    cwd: PROJECT_ROOT,
    stdio: ['ignore', 'pipe', 'pipe'],
  })
}

async function killHelper(proc: ChildProcess): Promise<void> {
  if (!proc.pid) return
  const exitPromise = new Promise<void>((resolve) => {
    proc.on('exit', () => resolve())
    proc.on('error', () => resolve())
  })
  try {
    proc.kill('SIGTERM')
  } catch {
    return
  }
  await Promise.race([exitPromise, sleep(5_000)])
}

function processStartToken(pid: number): string | null {
  try {
    return execSync(`ps -p ${pid} -o lstart=`, { encoding: 'utf-8', timeout: 2000 }).trim()
  } catch {
    return null
  }
}

function isProcessAlive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

test.describe('Helper supervision (PLA-301)', () => {
  let helper: ChildProcess | null = null

  test.afterEach(async () => {
    if (helper) {
      await killHelper(helper)
      helper = null
    }
  })

  test('spawn fake helper, verify health and props endpoints', async () => {
    helper = spawnHelper(HELPER_PORT, { model: 'acceptance-test-model' })
    await waitForHelper(HELPER_PORT)

    const healthRes = await fetch(`http://127.0.0.1:${HELPER_PORT}/health`)
    expect(healthRes.ok).toBe(true)
    const health = await healthRes.json()
    expect(health.status).toBe('ok')

    const propsRes = await fetch(`http://127.0.0.1:${HELPER_PORT}/props`)
    expect(propsRes.ok).toBe(true)
    const props = await propsRes.json()
    expect(props.model_path).toBe('acceptance-test-model')

    const modelsRes = await fetch(`http://127.0.0.1:${HELPER_PORT}/v1/models`)
    expect(modelsRes.ok).toBe(true)
    const models = await modelsRes.json()
    expect(models.data[0].id).toBe('acceptance-test-model')
  })

  test('SIGTERM causes clean exit', async () => {
    helper = spawnHelper(HELPER_PORT)
    await waitForHelper(HELPER_PORT)

    const pid = helper.pid!
    expect(isProcessAlive(pid)).toBe(true)

    const exitPromise = new Promise<number | null>((resolve) => {
      helper!.on('exit', (code) => resolve(code))
    })
    helper.kill('SIGTERM')
    await Promise.race([exitPromise, sleep(5_000).then(() => null)])

    expect(isProcessAlive(pid)).toBe(false)
    helper = null
  })

  test('birth token identifies the process across checks', async () => {
    helper = spawnHelper(HELPER_PORT)
    await waitForHelper(HELPER_PORT)

    const pid = helper.pid!
    const token1 = processStartToken(pid)
    expect(token1).toBeTruthy()

    // Same PID, same token a moment later
    await sleep(200)
    const token2 = processStartToken(pid)
    expect(token2).toBe(token1)

    // Kill, wait past the lstart second boundary, then spawn a new process.
    // lstart has second resolution, so spawning in the same second can
    // produce an identical token even with a different PID.
    await killHelper(helper)
    await sleep(1100)

    const helper2 = spawnHelper(HELPER_PORT + 1)
    await waitForHelper(HELPER_PORT + 1)
    const pid2 = helper2.pid!
    const token3 = processStartToken(pid2)
    expect(token3).toBeTruthy()

    if (pid !== pid2) {
      expect(token3).not.toBe(token1)
    }

    helper = helper2
  })

  test('replacement: kill old helper and spawn new one on same port', async () => {
    helper = spawnHelper(HELPER_PORT, { model: 'old-model' })
    await waitForHelper(HELPER_PORT)

    const oldPid = helper.pid!
    const oldToken = processStartToken(oldPid)

    // Verify old model
    const oldProps = await (await fetch(`http://127.0.0.1:${HELPER_PORT}/props`)).json()
    expect(oldProps.model_path).toBe('old-model')

    // Kill old, wait past lstart second boundary, spawn replacement
    await killHelper(helper)
    expect(isProcessAlive(oldPid)).toBe(false)
    await sleep(1100)

    helper = spawnHelper(HELPER_PORT, { model: 'new-model' })
    await waitForHelper(HELPER_PORT)

    const newPid = helper.pid!
    const newToken = processStartToken(newPid)

    // Verify replacement
    const newProps = await (await fetch(`http://127.0.0.1:${HELPER_PORT}/props`)).json()
    expect(newProps.model_path).toBe('new-model')

    // Birth tokens differ (new process)
    if (oldPid !== newPid) {
      expect(newToken).not.toBe(oldToken)
    }
  })

  test('unhealthy helper detected via failed health check', async () => {
    helper = spawnHelper(HELPER_PORT, { failHealth: true })

    // The process is alive but health check fails
    const deadline = Date.now() + 5_000
    while (Date.now() < deadline) {
      try {
        await fetch(`http://127.0.0.1:${HELPER_PORT}/health`, {
          signal: AbortSignal.timeout(1000),
        })
        break
      } catch {
        await sleep(100)
      }
    }

    const healthRes = await fetch(`http://127.0.0.1:${HELPER_PORT}/health`, {
      signal: AbortSignal.timeout(2000),
    })
    expect(healthRes.status).toBe(503)
  })

  test('slow-starting helper: health fails then succeeds', async () => {
    helper = spawnHelper(HELPER_PORT, { slowStart: 1.5 })

    // Initially unhealthy
    const earlyDeadline = Date.now() + 3_000
    let sawUnhealthy = false
    while (Date.now() < earlyDeadline) {
      try {
        const res = await fetch(`http://127.0.0.1:${HELPER_PORT}/health`, {
          signal: AbortSignal.timeout(500),
        })
        if (res.status === 503) {
          sawUnhealthy = true
        } else if (res.ok && sawUnhealthy) {
          break
        }
      } catch {
        // not bound yet
      }
      await sleep(200)
    }

    expect(sawUnhealthy).toBe(true)

    // Eventually healthy
    await waitForHelper(HELPER_PORT, 5_000)
    const healthRes = await fetch(`http://127.0.0.1:${HELPER_PORT}/health`)
    expect(healthRes.ok).toBe(true)
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
