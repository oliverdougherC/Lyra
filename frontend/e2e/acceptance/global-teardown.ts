/**
 * Acceptance global teardown -- stops every process and removes the temp dir.
 *
 * Runs after all specs complete (or after any crash/timeout).  Uses confirmed
 * exit waits (no fire-and-forget setTimeout) and verifies process identity
 * via birth tokens before signalling PIDs read from state files.
 */

import { execSync } from 'node:child_process'
import { readFile, rm, unlink } from 'node:fs/promises'

import { TutorFixture } from './tutor-fixture'
import { captureOwnedFixtures, survivingOwnedFixtures } from './process-ownership'

const SIGTERM_WAIT_MS = 5_000
const SIGKILL_WAIT_MS = 3_000

export default async function globalTeardown() {
  console.log('\n  Tearing down acceptance stack...')

  const failures: string[] = []
  const ownedPgids: number[] = []
  const ownedFixtures = new Map<number, string>()

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

  const frontendPort = Number(process.env.ACCEPTANCE_FRONTEND_PORT ?? 3000)

  if (mem) {
    // The persisted state file is authoritative for the CURRENT backend/frontend
    // lifetime. An in-suite restartBackend() runs inside a Playwright WORKER process:
    // it can replace the process and update the file, but it can never update this
    // (setup/teardown) process's ChildProcess references. If the file records a
    // different PID than the in-memory object, the in-memory object is stale -- its
    // process is already dead -- and the file's PID (verified by birth token) owns the
    // replacement. Preferring the stale object here is exactly how the replacement
    // backend previously escaped ownership and had to be swept as an orphan.
    const persisted = await readPersistedState(mem.stateFile)
    ownedPgids.push(
      ...(persisted
        ? verifiedStateRoots(persisted)
        : [mem.backend, mem.frontend]
            .filter((child) => child?.pid && child.exitCode === null && child.signalCode === null)
            .map((child) => child.pid!)),
    )
    for (const [pid, token] of captureOwnedFixtures(ownedPgids)) ownedFixtures.set(pid, token)

    if (persisted && mem.frontend?.pid && persisted.frontendPid !== mem.frontend.pid) {
      console.log(
        `  frontend pid changed in-suite (${mem.frontend.pid} -> ${persisted.frontendPid}); using persisted state`,
      )
      await verifyAndKill(persisted.frontendPid, persisted.frontendStartToken, 'frontend')
    } else {
      await killAndWait(mem.frontend, 'frontend')
    }

    if (persisted && mem.backend?.pid && persisted.backendPid !== mem.backend.pid) {
      console.log(
        `  backend was restarted in-suite (pid ${mem.backend.pid} -> ${persisted.backendPid}); using persisted state`,
      )
      await verifyAndKill(persisted.backendPid, persisted.backendStartToken, 'backend')
    } else {
      await killAndWait(mem.backend, 'backend')
    }

    // Prove the stop, don't assume it: the owned ports must stop serving and no member
    // of the owned process groups (detached leaders, so pgid == recorded pid) may remain.
    await verifyPortStopped(backendPort, 'backend', failures)
    await verifyPortStopped(frontendPort, 'frontend', failures)
    verifyNoGroupMembers(ownedPgids, failures)

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
    await cleanupFromStateFiles(ownedFixtures)
  }

  // Final sweep is restricted to this run: another acceptance checkout may be active.
  // The process-group kills above should have reclaimed everything, but the
  // zero-orphan gate requires proof, not trust. Any acceptance fixture still alive after
  // teardown (a uvicorn that outlived its group signal, a fake-helper whose parent died)
  // is killed here only when its captured birth token still proves this run owns it.
  const orphanCount = await sweepOrphanedFixtures(ownedFixtures)
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

async function sweepOrphanedFixtures(owned: Map<number, string>): Promise<number> {
  const orphans = survivingOwnedFixtures(owned).filter((pid) => pid !== process.pid)

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
    if (!survivingOwnedFixtures(new Map([[pid, owned.get(pid) ?? '']])).includes(pid)) continue
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
/*  Persisted state (authoritative for in-suite restarts)              */
/* ------------------------------------------------------------------ */

interface PersistedState {
  dataDir: string
  backendPid: number
  frontendPid: number
  backendStartToken: string | null
  frontendStartToken: string | null
}

function verifiedStateRoots(state: PersistedState): number[] {
  const identities = new Map<number, string>()
  if (state.backendStartToken) identities.set(state.backendPid, state.backendStartToken)
  if (state.frontendStartToken) identities.set(state.frontendPid, state.frontendStartToken)
  return survivingOwnedFixtures(identities)
}

async function readPersistedState(path: string): Promise<PersistedState | null> {
  try {
    const raw = await readFile(path, 'utf-8')
    return JSON.parse(raw) as PersistedState
  } catch {
    return null
  }
}

/** Fail teardown if the given port is still serving after the owned process was stopped. */
async function verifyPortStopped(port: number, label: string, failures: string[]): Promise<void> {
  const deadline = Date.now() + 5_000
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/`, { signal: AbortSignal.timeout(1000) })
      void res.body?.cancel()
    } catch {
      console.log(`  ${label} port ${port} no longer served`)
      return
    }
    await sleep(250)
  }
  failures.push(`${label} port ${port} is still serving after teardown stop`)
}

/**
 * Fail teardown if any process still belongs to an owned process group. The owned
 * children were spawned detached (pgid == recorded leader pid), so a surviving member
 * means the group signal did not reclaim the whole tree. Matching is restricted to
 * fixture-shaped commands so a recycled pgid can never implicate an unrelated process.
 */
function verifyNoGroupMembers(pgids: number[], failures: string[]): void {
  if (pgids.length === 0) return
  let listing: string
  try {
    listing = execSync('ps -axww -o pid=,pgid=,command=', { encoding: 'utf-8', timeout: 5000 })
  } catch {
    return
  }
  const fixtureShapes = [
    'acceptance.backend_harness:app',
    'e2e/acceptance/fake-helper.py',
    'uvicorn',
    'vite preview',
  ]
  const survivors: string[] = []
  for (const line of listing.split('\n')) {
    const m = line.trim().match(/^(\d+)\s+(\d+)\s+(.*)$/)
    if (!m) continue
    const [, pidStr, pgidStr, command] = m
    if (Number(pidStr) === process.pid) continue
    if (!pgids.includes(Number(pgidStr))) continue
    if (!fixtureShapes.some((s) => command.includes(s))) continue
    survivors.push(`pid ${pidStr} (pgid ${pgidStr}): ${command.slice(0, 120)}`)
  }
  if (survivors.length > 0) {
    failures.push(
      `${survivors.length} owned process-group member(s) survived teardown:\n    ${survivors.join('\n    ')}`,
    )
  } else {
    console.log('  No owned process-group members remain')
  }
}

/* ------------------------------------------------------------------ */
/*  State file fallback                                                */
/* ------------------------------------------------------------------ */

async function cleanupFromStateFiles(owned: Map<number, string>) {
  const filePath = process.env.ACCEPTANCE_STATE_FILE
  if (!filePath) {
    console.log('  No current-run state file; leaving other acceptance runs alone')
    return
  }
  const state = await readPersistedState(filePath)
  if (!state) return
  for (const [pid, token] of captureOwnedFixtures(verifiedStateRoots(state))) {
    owned.set(pid, token)
  }
  await verifyAndKill(state.frontendPid, state.frontendStartToken, 'frontend')
  await verifyAndKill(state.backendPid, state.backendStartToken, 'backend')
  await cleanDataDir(state.dataDir)
  await unlink(filePath)
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
 *
 * Signals the whole process GROUP (-pid): every fixture recorded in the state file was
 * spawned detached, so its pid is a group leader (pgid == pid) and the real uvicorn
 * python / next worker grandchildren live in that group. Signalling the wrapper pid
 * alone orphans them -- the exact leak the zero-orphan gate forbids. Falls back to the
 * bare pid if the group is already gone.
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

  const signalGroup = (sig: NodeJS.Signals): boolean => {
    try {
      process.kill(-pid, sig)
      return true
    } catch {
      try {
        process.kill(pid, sig)
        return true
      } catch {
        return false
      }
    }
  }

  if (!signalGroup('SIGTERM')) {
    console.log(`  ${label} (pid ${pid}) already stopped`)
    return
  }
  console.log(`  Sent SIGTERM to ${label} group (pid ${pid})`)

  const deadline = Date.now() + SIGTERM_WAIT_MS
  while (Date.now() < deadline) {
    if (!isProcessAlive(pid)) {
      console.log(`  ${label} stopped`)
      return
    }
    await sleep(200)
  }

  console.log(`  ${label} did not exit after SIGTERM, sending SIGKILL`)
  signalGroup('SIGKILL')

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
