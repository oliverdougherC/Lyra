/**
 * Acceptance global teardown — stops every process and removes the temp dir.
 *
 * Runs after all specs complete (or after any crash/timeout).  Reads the state
 * file written by global-setup and kills processes by PID, then removes the
 * isolated data directory.
 */

import { readFile, rm, unlink } from 'node:fs/promises'
import { resolve, join } from 'node:path'

import { TutorFixture } from './tutor-fixture'

const PROJECT_ROOT = resolve(__dirname, '..', '..', '..')
const STATE_FILE = join(PROJECT_ROOT, '.acceptance-state.json')

export default async function globalTeardown() {
  console.log('\n  Tearing down acceptance stack...')

  // Try the in-memory references first (fastest, most reliable)
  const mem = (globalThis as Record<string, unknown>).__acceptanceState as
    | {
        tutor: TutorFixture
        backend: import('node:child_process').ChildProcess
        frontend: import('node:child_process').ChildProcess
        dataDir: string
      }
    | undefined

  if (mem) {
    killProc(mem.frontend, 'frontend')
    killProc(mem.backend, 'backend')
    try {
      await mem.tutor.stop()
      console.log('  Tutor fixture stopped')
    } catch {
      /* already gone */
    }
    await cleanDataDir(mem.dataDir)
  } else {
    // Fall back to the state file (e.g. if setup and teardown run in
    // separate processes, which some Playwright shard modes do).
    try {
      const raw = await readFile(STATE_FILE, 'utf-8')
      const state = JSON.parse(raw) as {
        dataDir: string
        backendPid: number
        frontendPid: number
        tutorPort: number
      }

      killPid(state.frontendPid, 'frontend')
      killPid(state.backendPid, 'backend')

      // The tutor fixture is an in-process server; if we only have PIDs we
      // don't have a handle. Killing the tutor port listener is sufficient
      // because the fixture is part of the setup process tree.

      await cleanDataDir(state.dataDir)
    } catch (err) {
      console.warn('  Could not read state file; manual cleanup may be needed:', err)
    }
  }

  // Remove the state file itself
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

function killProc(proc: import('node:child_process').ChildProcess | undefined, label: string) {
  if (!proc?.pid) return
  try {
    // SIGTERM first, then SIGKILL after a short delay
    proc.kill('SIGTERM')
    setTimeout(() => {
      try {
        proc.kill('SIGKILL')
      } catch {
        /* already gone */
      }
    }, 3000)
    console.log(`  Sent SIGTERM to ${label} (pid ${proc.pid})`)
  } catch {
    console.log(`  ${label} already stopped`)
  }
}

function killPid(pid: number, label: string) {
  try {
    process.kill(pid, 'SIGTERM')
    setTimeout(() => {
      try {
        process.kill(pid, 'SIGKILL')
      } catch {
        /* already gone */
      }
    }, 3000)
    console.log(`  Sent SIGTERM to ${label} (pid ${pid})`)
  } catch {
    console.log(`  ${label} (pid ${pid}) already stopped`)
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
