/**
 * Acceptance global teardown -- stops every process and removes the temp dir.
 *
 * Runs after all specs complete (or after any crash/timeout).  Uses confirmed
 * exit waits (no fire-and-forget setTimeout) and verifies process identity
 * before signalling PIDs read from the state file.
 */

import { execSync } from 'node:child_process'
import { readFile, rm, unlink } from 'node:fs/promises'
import { resolve, join } from 'node:path'

import { TutorFixture } from './tutor-fixture'

const PROJECT_ROOT = resolve(__dirname, '..', '..', '..')
const STATE_FILE = join(PROJECT_ROOT, '.acceptance-state.json')

const SIGTERM_WAIT_MS = 5_000
const SIGKILL_WAIT_MS = 3_000

export default async function globalTeardown() {
  console.log('\n  Tearing down acceptance stack...')

  const mem = (globalThis as Record<string, unknown>).__acceptanceState as
    | {
        tutor: TutorFixture
        backend: import('node:child_process').ChildProcess
        frontend: import('node:child_process').ChildProcess
        dataDir: string
      }
    | undefined

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
  } else {
    // Fall back to the state file.  Verify each PID is still ours before
    // signalling -- a recycled PID could belong to an unrelated process.
    try {
      const raw = await readFile(STATE_FILE, 'utf-8')
      const state = JSON.parse(raw) as {
        dataDir: string
        backendPid: number
        frontendPid: number
        tutorPort: number
        startedAt: number
      }

      await verifyAndKill(state.frontendPid, 'node', 'frontend')
      await verifyAndKill(state.backendPid, 'python', 'backend')
      await cleanDataDir(state.dataDir)
    } catch (err) {
      console.warn('  Could not read state file; manual cleanup may be needed:', err)
    }
  }

  try {
    await unlink(STATE_FILE)
  } catch {
    /* already gone */
  }

  console.log('  Teardown complete\n')
}

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

/**
 * Send SIGTERM and wait for the child to exit.  If it does not exit within
 * SIGTERM_WAIT_MS, escalate to SIGKILL and wait again.  Returns only after
 * the process has actually exited (or was already gone).
 */
async function killAndWait(
  proc: import('node:child_process').ChildProcess | undefined,
  label: string,
): Promise<void> {
  if (!proc?.pid) return
  if (proc.exitCode !== null || proc.signalCode !== null) {
    console.log(`  ${label} already exited`)
    return
  }

  const exitPromise = new Promise<void>((resolve) => {
    proc.on('exit', () => resolve())
    proc.on('error', () => resolve())
  })

  try {
    proc.kill('SIGTERM')
    console.log(`  Sent SIGTERM to ${label} (pid ${proc.pid})`)
  } catch {
    console.log(`  ${label} already stopped`)
    return
  }

  const termResult = await Promise.race([
    exitPromise.then(() => 'exited' as const),
    sleep(SIGTERM_WAIT_MS).then(() => 'timeout' as const),
  ])

  if (termResult === 'timeout') {
    console.log(`  ${label} did not exit after SIGTERM, sending SIGKILL`)
    try {
      proc.kill('SIGKILL')
    } catch {
      /* already gone */
    }
    await Promise.race([exitPromise, sleep(SIGKILL_WAIT_MS)])
  }

  console.log(`  ${label} stopped`)
}

/**
 * Verify that a PID from the state file still belongs to a process we
 * started (by checking its command name) before signalling it.  This
 * prevents killing an unrelated process if the PID was recycled.
 */
async function verifyAndKill(pid: number, expectedCmd: string, label: string): Promise<void> {
  if (!isProcessOurs(pid, expectedCmd)) {
    console.log(`  ${label} (pid ${pid}): not ours or already gone, skipping`)
    return
  }

  try {
    process.kill(pid, 'SIGTERM')
    console.log(`  Sent SIGTERM to ${label} (pid ${pid})`)
  } catch {
    console.log(`  ${label} (pid ${pid}) already stopped`)
    return
  }

  // Wait for exit by polling (we don't have the ChildProcess handle)
  const deadline = Date.now() + SIGTERM_WAIT_MS
  while (Date.now() < deadline) {
    if (!isProcessAlive(pid)) {
      console.log(`  ${label} stopped`)
      return
    }
    await sleep(200)
  }

  // Escalate
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
  console.log(`  ${label} stopped`)
}

function isProcessAlive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

function isProcessOurs(pid: number, expectedCmd: string): boolean {
  try {
    const cmd = execSync(`ps -p ${pid} -o comm=`, { encoding: 'utf-8', timeout: 2000 }).trim()
    return cmd.toLowerCase().includes(expectedCmd.toLowerCase())
  } catch {
    return false
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
