/**
 * Helper process supervision decisions through the PRODUCTION LlamaServer (PLA-301).
 *
 * The fake helper may be a test fixture, but every ownership/adoption DECISION below is
 * made by a fresh production `LlamaServer.ensure_running()` instance -- not by the
 * harness. Direct fixture spawning is used only to construct a deliberately FOREIGN
 * process (external-compatible / wrong-model); the behavior under assertion is always the
 * production supervisor's.
 *
 * Scenarios:
 *   1. Valid survivor adoption: durable ownership exists, a fresh supervisor adopts the
 *      same port/model + valid PID/birth token rather than spawning another; stop()
 *      reclaims it.
 *   2. Stale ownership: durable record has a wrong birth token for a live foreign PID;
 *      the production supervisor does NOT claim/kill the unrelated process and reconciles
 *      the stale record safely.
 *   3. Compatible external same-model server: healthy, correct model, no valid Lyra
 *      ownership record -> used as external-compatible; stop() must NOT kill it.
 *   4. Wrong-model / foreign port owner: healthy on the helper port but wrong model ->
 *      production refuses (ConfigurationError); the foreign process remains alive.
 *
 * Every fixture process is cleaned up deterministically inside its own test (plus a
 * per-test safety net) so no orphan survives and each test starts from a free port.
 */

import { test, expect } from '@playwright/test'
import { BACKEND } from './helpers'

const HEADERS: Record<string, string> = {
  'Content-Type': 'application/json',
  'X-Lyra-Client': 'acceptance-test',
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}

/** Response shape for the harness scenario/helper control endpoints (subset used here). */
interface ScenarioResponse {
  ok?: boolean
  error?: string
  spawned_pid?: number | null
  adopted_pid?: number | null
  healthy?: boolean
  alive?: boolean
  pid?: number | null
  ownership_record?: Record<string, unknown> | null
}

async function j(method: string, path: string, body?: unknown): Promise<ScenarioResponse> {
  const res = await fetch(`${BACKEND}${path}`, {
    method,
    headers: HEADERS,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  return (await res.json()) as ScenarioResponse
}

async function ensureRunning(
  displayName: string,
  model: string,
  resetPrevious = false,
): Promise<ScenarioResponse> {
  return j('POST', '/_acceptance/scenario/ensure-running', {
    display_name: displayName,
    model,
    reset_previous: resetPrevious,
  })
}
async function scenarioStop(): Promise<ScenarioResponse> {
  return j('POST', '/_acceptance/scenario/stop')
}
async function writeStaleRecord(
  displayName: string,
  pid: number,
  token: string,
): Promise<ScenarioResponse> {
  return j('POST', '/_acceptance/scenario/write-stale-record', {
    display_name: displayName,
    pid,
    start_token: token,
  })
}
async function killPort(): Promise<ScenarioResponse> {
  return j('POST', '/_acceptance/scenario/kill-port')
}
async function pidAlive(pid: number): Promise<boolean> {
  const res = await j('GET', `/_acceptance/scenario/pid-alive/${pid}`)
  return Boolean(res.alive)
}

/** True when nothing is serving the helper port (the reclaim signal). */
async function portNotServing(): Promise<boolean> {
  try {
    const res = await fetch('http://127.0.0.1:19500/health', { cache: 'no-store' })
    return res.status !== 200
  } catch {
    // Connection refused / reset means the port is no longer serving.
    return true
  }
}

/**
 * Spawn a deliberately FOREIGN helper (an external process the supervisor must reason
 * about) through the harness, which tracks it for deterministic cleanup. Returns the
 * exact listener PID. This is acceptable fixture spawning to construct an external
 * process; every adoption/ownership decision under assertion is still made by the
 * production supervisor.
 */
async function spawnForeignHelper(model: string): Promise<number> {
  const res = await j('POST', '/_acceptance/scenario/spawn-foreign', { model })
  if (!res.ok) throw new Error(`Could not bring up foreign helper: ${res.error}`)
  return res.pid as number
}

async function killForeign(): Promise<ScenarioResponse> {
  return j('POST', '/_acceptance/scenario/kill-foreign')
}

/** Deterministic per-test reset: drop scenario supervisor + foreign helper, free port. */
async function hardReset(): Promise<void> {
  await scenarioStop()
  await killForeign()
  await killPort()
  await sleep(300)
}

/** Full isolation reset: free the port (kill any listener), clear BOTH ownership records. */
async function fullCleanup(): Promise<void> {
  await j('POST', '/_acceptance/helper/cleanup')
  await sleep(200)
}

test.describe('Helper supervision decisions (PLA-301)', () => {
  // Guarantee every test starts from a free port with no stale ownership record, regardless of
  // how a prior spec/test left the fixture (a foreign wrong-model helper, an adopted survivor,
  // or a leftover record). Without this, one spec's deliberately-left-alive foreign process gets
  // ADOPTED by the next test's supervisor instead of being replaced.
  test.beforeEach(async () => {
    await fullCleanup()
  })

  test.afterEach(async () => {
    await hardReset()
  })

  test('valid survivor adoption: fresh supervisor adopts, does not spawn; stop reclaims', async () => {
    // Production supervisor #1 spawns the helper and writes durable ownership.
    const start = await ensureRunning('acc-scenario', 'adopt-model', true)
    expect(start.ok).toBe(true)
    const firstPid = start.spawned_pid as number
    expect(firstPid).toBeTruthy()

    // Simulate a backend crash WITHOUT graceful supervisor shutdown: the supervisor
    // object is dropped but its helper -- an independent process group with a durable
    // ownership record -- survives. A fresh production supervisor now runs
    // ensure_running() against that state and must adopt it.
    const adopt = await ensureRunning('acc-scenario', 'adopt-model')
    expect(adopt.ok).toBe(true)
    // It adopted the survivor (same PID) rather than spawning a second process.
    expect(adopt.spawned_pid).toBeNull()
    expect(adopt.adopted_pid).toBe(firstPid)
    expect(adopt.healthy).toBe(true)

    // Exactly one helper is serving on the port (no duplicate spawned).
    const models = await (await fetch('http://127.0.0.1:19500/v1/models')).json()
    expect(models.data.length).toBe(1)

    // Production stop() reclaims the adopted survivor. The termination path SIGTERMs the group,
    // waits up to a 5s grace period, then SIGKILLs; under full-suite load that can take longer than
    // a fixed sleep, so poll for the RECLAIM rather than asserting on one point in time.
    //
    // We assert on production-faithful signals, NOT a bare PID liveness check: `spawned_pid` is
    // the `uv run python` wrapper PID (the real listener is its child), and on a busy machine that
    // PID can be recycled by an unrelated process, making os.kill(pid,0) a false positive. The two
    // authoritative signals that stop() reclaimed it are (a) the helper port stops serving, and
    // (b) the durable ownership record is removed -- which production only does after confirming
    // death via the birth token.
    await scenarioStop()
    const deadline = Date.now() + 15_000
    let portFree = false
    while (Date.now() < deadline) {
      if (await portNotServing()) {
        portFree = true
        break
      }
      await sleep(250)
    }
    expect(portFree).toBe(true)
    // The durable record is gone: production removed it only after confirming the process dead.
    const status = await j('GET', '/_acceptance/helper/status')
    expect(status.ownership_record ?? null).toBeNull()
  })

  test('stale ownership: wrong birth token for a live foreign PID is not claimed or killed', async () => {
    // A foreign process owns the helper port (constructed directly).
    const foreignPid = await spawnForeignHelper('foreign-model')

    // Write a STALE durable record: it points at the live foreign PID but with a WRONG
    // birth token, so its identity does not match.
    await writeStaleRecord('acc-scenario', foreignPid, 'proc:stale-bogus-token')

    // The production supervisor must NOT claim or kill the unrelated process; it
    // reconciles the stale record safely and proceeds (treating the healthy port as an
    // external-compatible server).
    const result = await ensureRunning('acc-scenario', 'foreign-model')
    expect(result.ok).toBe(true)
    // It did not spawn a duplicate and did not adopt the foreign PID.
    expect(result.spawned_pid).toBeNull()
    expect(result.adopted_pid ?? null).not.toBe(foreignPid)

    // The unrelated foreign process is still alive (never killed by the supervisor).
    expect(await pidAlive(foreignPid)).toBe(true)

    // Production stop() must not kill the external-compatible foreign process either.
    await scenarioStop()
    await sleep(300)
    expect(await pidAlive(foreignPid)).toBe(true)
  })

  test('compatible external same-model server is used but never killed by stop()', async () => {
    // A healthy process serves the CORRECT model but has NO valid Lyra ownership record.
    const foreignPid = await spawnForeignHelper('external-same-model')

    const result = await ensureRunning('acc-scenario', 'external-same-model')
    expect(result.ok).toBe(true)
    // Used as external-compatible: no spawn, and no adoption (no valid ownership record).
    expect(result.spawned_pid).toBeNull()
    expect(result.adopted_pid ?? null).not.toBe(foreignPid)

    // Production stop() must NOT kill the external server.
    await scenarioStop()
    await sleep(300)
    expect(await pidAlive(foreignPid)).toBe(true)
  })

  test('wrong-model or foreign port owner is refused; the foreign process stays alive', async () => {
    // Something healthy answers on the helper port but serves the WRONG model.
    const foreignPid = await spawnForeignHelper('totally-wrong-model')

    // The production supervisor refuses to use a port serving a different model.
    const result = await ensureRunning('acc-scenario', 'expected-model')
    expect(result.ok).toBe(false)
    expect(String(result.error)).toMatch(/serving the model|not the .*server/i)

    // The foreign process remains alive (the supervisor did not touch it).
    expect(await pidAlive(foreignPid)).toBe(true)
  })
})
