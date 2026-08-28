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

  const mem = (globalThis as Record<string, unknown>).__acceptanceState as
    | {
        tutor: TutorFixture
        backend: import('node:child_process').ChildProcess
        frontend: import('node:child_process').ChildProcess
        dataDir: string
        stateFile: string
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
    try {
      await unlink(mem.stateFile)
    } catch {
      /* already gone */
    }
  } else {
    // Fall back to scanning for state files from this or prior runs.
    await cleanupFromStateFiles()
  }

  console.log('  Teardown complete\n')
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
