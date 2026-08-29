/**
 * Acceptance global teardown -- stops every process and removes the temp dir.
 *
 * Runs after all specs complete (or after any crash/timeout).  Uses confirmed
 * exit waits (no fire-and-forget setTimeout) and verifies process identity
 * via birth tokens before signalling PIDs read from state files.
 */

import { execSync } from 'node:child_process'
import { readdir, readFile, rm, unlink } from 'node:fs/promises'
import { resolve, join } from 'node:path'

import { TutorFixture } from './tutor-fixture'

const PROJECT_ROOT = resolve(__dirname, '..', '..', '..')

const SIGTERM_WAIT_MS = 5_000
const SIGKILL_WAIT_MS = 3_000

export default async function globalTeardown() {
  console.log('\n  Tearing down acceptance stack...')

  const failures: string[] = []

  const mem = (globalThis as Record<string, unknown>).__acceptanceState as
    | {
        tutor: TutorFixture
        backend: import('node:child_process').ChildProcess
        frontend: import('node:child_process').ChildProcess
        dataDir: string
        stateFile: string
      }
    | undefined

  // Query the backend-failure ledger BEFORE killing the backend. This is the
  // authoritative second line of defense: even if the zz- spec passed, teardown
  // independently verifies zero unconsumed failures.
  const backendPort = Number(process.env.ACCEPTANCE_BACKEND_PORT ?? 8000)
  try {
    const res = await fetch(`http://127.0.0.1:${backendPort}/_acceptance/backend-failures`, {
      signal: AbortSignal.timeout(5_000),
    })
    if (res.ok) {
      const snap = (await res.json()) as {
        unconsumed_count: number
        unconsumed: Array<Record<string, unknown>>
        total_recorded: number
        consumed: number
      }
      if (snap.unconsumed_count > 0) {
        const lines = snap.unconsumed.slice(0, 5).map((f) => {
          return `[${f.method}] ${f.route} -> ${f.kind} status=${f.status ?? '-'} exc=${f.exc_type ?? '-'}`
        })
        failures.push(
          `${snap.unconsumed_count} unconsumed backend failure(s):\n    ${lines.join('\n    ')}`,
        )
      }
      console.log(
        `  Backend failure ledger: ${snap.total_recorded} recorded, ${snap.consumed} consumed, ${snap.unconsumed_count} unconsumed`,
      )
    }
  } catch {
    console.log('  Could not query backend failure ledger (backend may already be down)')
  }

  if (mem) {
    await killAndWait(mem.frontend, 'frontend')
    await killAndWait(mem.backend, 'backend')
    try {
      await mem.tutor.stop()
      console.log('  Tutor fixture stopped')
    } catch {
      /* already gone */
    }
    await cleanDataDir(mem.dataDir)
    try {
      await unlink(mem.stateFile)
    } catch {
      /* already gone */
    }
  } else {
    // Fall back to scanning for state files from this or prior runs.
    await cleanupFromStateFiles()
  }

  // Final sweep: the process-group kills above should have reclaimed everything, but the
  // zero-orphan gate requires proof, not trust. Any acceptance fixture still alive after
  // teardown (a uvicorn that outlived its group signal, a fake-helper whose parent died)
  // is killed here and the run is FAILED. The matchers are unique to acceptance fixtures --
  // production Lyra runs `backend.main:app`, never `acceptance.backend_harness:app` or
  // `fake-helper.py` -- so a user's real server can never be touched.
  const orphanCount = await sweepOrphanedFixtures()
  if (orphanCount > 0) {
    failures.push(`${orphanCount} orphaned fixture process(es) required cleanup`)
  }

  if (failures.length > 0) {
    console.error(`\n  TEARDOWN INVARIANT VIOLATIONS:\n    ${failures.join('\n    ')}\n`)
    throw new Error(
      `Acceptance teardown FAILED: ${failures.length} invariant violation(s).\n` +
        failures.map((f) => `  - ${f}`).join('\n'),
    )
  }

  console.log('  Teardown complete\n')
}

/* ------------------------------------------------------------------ */
/*  Orphan fixture sweep                                               */
/* ------------------------------------------------------------------ */

// Command-line signatures that identify an acceptance fixture process and nothing else.
const ORPHAN_PATTERNS = ['acceptance.backend_harness:app', 'e2e/acceptance/fake-helper.py']

async function sweepOrphanedFixtures(): Promise<number> {
  let listing: string
  try {
    listing = execSync('ps -axww -o pid=,command=', { encoding: 'utf-8', timeout: 5000 })
  } catch {
    return 0
  }

  const orphans: number[] = []
  for (const line of listing.split('\n')) {
    if (!ORPHAN_PATTERNS.some((p) => line.includes(p))) continue
    const pidField = line.trim().split(/\s+/)[0]
    if (!pidField || Number(pidField) === process.pid) continue
    orphans.push(Number(pidField))
  }

  if (orphans.length === 0) return 0

  for (const pid of orphans) {
    try {
      process.kill(pid, 'SIGTERM')
    } catch {
      /* already gone */
    }
  }
  console.log(`  Sweeping ${orphans.length} orphaned fixture process(es): ${orphans.join(', ')}`)

  const deadline = Date.now() + SIGTERM_WAIT_MS
  while (Date.now() < deadline) {
    if (orphans.every((pid) => !isProcessAlive(pid))) break
    await sleep(200)
  }
  for (const pid of orphans) {
    if (!isProcessAlive(pid)) continue
    try {
      process.kill(pid, 'SIGKILL')
    } catch {
      /* already gone */
    }
  }
  const killDeadline = Date.now() + SIGKILL_WAIT_MS
  while (Date.now() < killDeadline) {
    if (orphans.every((pid) => !isProcessAlive(pid))) break
    await sleep(200)
  }
  const survivors = orphans.filter((pid) => isProcessAlive(pid))
  if (survivors.length > 0) {
    console.error(`  Orphan sweep: ${survivors.length} survived SIGKILL: ${survivors.join(', ')}`)
  } else {
    console.log(`  Orphan sweep: ${orphans.length} reclaimed, all verified dead`)
  }

  return orphans.length
}

/* ------------------------------------------------------------------ */
/*  State file fallback                                                */
/* ------------------------------------------------------------------ */

async function cleanupFromStateFiles() {
  try {
    const entries = await readdir(PROJECT_ROOT)
    const stateFiles = entries.filter(
      (e) => e.startsWith('.acceptance-state-') && e.endsWith('.json'),
    )

    if (stateFiles.length === 0) {
      console.log('  No state files found; nothing to clean up')
      return
    }

    for (const file of stateFiles) {
      const filePath = join(PROJECT_ROOT, file)
      try {
        const raw = await readFile(filePath, 'utf-8')
        const state = JSON.parse(raw) as {
          dataDir: string
          backendPid: number
          frontendPid: number
          backendStartToken: string | null
          frontendStartToken: string | null
        }

        await verifyAndKill(state.frontendPid, state.frontendStartToken, 'frontend')
        await verifyAndKill(state.backendPid, state.backendStartToken, 'backend')
        await cleanDataDir(state.dataDir)
        await unlink(filePath)
      } catch (err) {
        console.warn(`  Could not process state file ${file}:`, err)
      }
    }
  } catch (err) {
    console.warn('  Could not scan for state files; manual cleanup may be needed:', err)
  }
}

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

async function killAndWait(
  proc: import('node:child_process').ChildProcess | undefined,
  label: string,
): Promise<void> {
  if (!proc?.pid) return
  if (proc.exitCode !== null || proc.signalCode !== null) {
    console.log(`  ${label} already exited`)
    return
  }
  const pid = proc.pid

  const exitPromise = new Promise<void>((resolve) => {
    proc.on('exit', () => resolve())
    proc.on('error', () => resolve())
  })

  // The child was spawned detached (its own process group, pgid == pid), so signal the
  // whole group: that reclaims the real uvicorn python / next worker grandchild processes
  // that outlive the `uv` / `pnpm exec` wrapper. Signalling the wrapper pid alone orphans
  // them -- exactly the leakage the zero-orphan gate forbids.
  const signalGroup = (sig: NodeJS.Signals): boolean => {
    try {
      process.kill(-pid, sig)
      return true
    } catch {
      try {
        proc.kill(sig)
        return true
      } catch {
        return false
      }
    }
  }

  if (!signalGroup('SIGTERM')) {
    console.log(`  ${label} already stopped`)
    return
  }
  console.log(`  Sent SIGTERM to ${label} group (pid ${pid})`)

  const termResult = await Promise.race([
    exitPromise.then(() => 'exited' as const),
    sleep(SIGTERM_WAIT_MS).then(() => 'timeout' as const),
  ])

  if (termResult === 'timeout') {
    console.log(`  ${label} did not exit after SIGTERM, sending SIGKILL`)
    signalGroup('SIGKILL')
    await Promise.race([exitPromise, sleep(SIGKILL_WAIT_MS)])
  }

  if (proc.exitCode === null && proc.signalCode === null) {
    console.error(`  ${label} (pid ${pid}) could not be proven dead`)
  } else {
    console.log(`  ${label} stopped`)
  }
}

/**
 * Verify that a PID from the state file still belongs to the process we
 * started (by comparing its birth token) before signalling it.
 */
async function verifyAndKill(
  pid: number,
  expectedToken: string | null,
  label: string,
): Promise<void> {
  if (!isProcessAlive(pid)) {
    console.log(`  ${label} (pid ${pid}): already gone`)
    return
  }

  if (expectedToken) {
    const currentToken = processStartToken(pid)
    if (currentToken && currentToken !== expectedToken) {
      console.log(`  ${label} (pid ${pid}): PID recycled (token mismatch), skipping`)
      return
    }
  }

  try {
    process.kill(pid, 'SIGTERM')
    console.log(`  Sent SIGTERM to ${label} (pid ${pid})`)
  } catch {
    console.log(`  ${label} (pid ${pid}) already stopped`)
    return
  }

  const deadline = Date.now() + SIGTERM_WAIT_MS
  while (Date.now() < deadline) {
    if (!isProcessAlive(pid)) {
      console.log(`  ${label} stopped`)
      return
    }
    await sleep(200)
  }

  console.log(`  ${label} did not exit after SIGTERM, sending SIGKILL`)
  try {
    process.kill(pid, 'SIGKILL')
  } catch {
    /* already gone */
  }

  const killDeadline = Date.now() + SIGKILL_WAIT_MS
  while (Date.now() < killDeadline) {
    if (!isProcessAlive(pid)) break
    await sleep(200)
  }
  if (isProcessAlive(pid)) {
    console.error(`  ${label} (pid ${pid}) could not be proven dead`)
  } else {
    console.log(`  ${label} stopped`)
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

function processStartToken(pid: number): string | null {
  try {
    return execSync(`ps -p ${pid} -o lstart=`, { encoding: 'utf-8', timeout: 2000 }).trim()
  } catch {
    return null
  }
}

async function cleanDataDir(dir: string) {
  if (!dir || !dir.includes('lyra-acceptance')) {
    console.warn(`  Refusing to remove suspicious data dir: ${dir}`)
    return
  }
  try {
    await rm(dir, { recursive: true, force: true })
    console.log(`  Removed data dir ${dir}`)
  } catch {
    console.warn(`  Could not remove data dir ${dir}`)
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}
