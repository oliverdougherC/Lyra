import { execFileSync, spawn, type ChildProcess } from 'node:child_process'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, expect, it, vi } from 'vitest'
import globalTeardown from '../e2e/acceptance/global-teardown'

const children: ChildProcess[] = []
const directories: string[] = []
const originalStateFile = process.env.ACCEPTANCE_STATE_FILE
async function fixture() {
  const child = spawn(
    process.execPath,
    ['-e', 'setInterval(() => {}, 1000)', 'acceptance.backend_harness:app'],
    { detached: true, stdio: 'ignore' },
  )
  children.push(child)
  await new Promise<void>((resolve, reject) => {
    child.once('spawn', resolve)
    child.once('error', reject)
  })
  return child
}
function alive(child: ChildProcess) {
  try {
    process.kill(child.pid!, 0)
    return true
  } catch {
    return false
  }
}
async function stateFor(backend: ChildProcess, frontend: ChildProcess) {
  const dataDir = await mkdtemp(join(tmpdir(), 'lyra-acceptance-teardown-'))
  directories.push(dataDir)
  const stateFile = `${dataDir}.json`
  const token = (child: ChildProcess) =>
    execFileSync('ps', ['-p', String(child.pid), '-o', 'lstart='], { encoding: 'utf8' }).trim()
  await writeFile(
    stateFile,
    JSON.stringify({
      dataDir,
      backendPid: backend.pid,
      frontendPid: frontend.pid,
      backendStartToken: token(backend),
      frontendStartToken: token(frontend),
    }),
  )
  return { dataDir, stateFile }
}
function ledger(unconsumed = 0) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (!url.endsWith('/_acceptance/backend-failures')) throw new Error('port stopped')
      return {
        ok: true,
        json: async () => ({
          unconsumed_count: unconsumed,
          unconsumed: [],
          total_recorded: unconsumed,
          consumed: 0,
        }),
      }
    }),
  )
}
afterEach(async () => {
  delete (globalThis as Record<string, unknown>).__acceptanceState
  if (originalStateFile === undefined) delete process.env.ACCEPTANCE_STATE_FILE
  else process.env.ACCEPTANCE_STATE_FILE = originalStateFile
  vi.unstubAllGlobals()
  for (const child of children.splice(0)) {
    try {
      process.kill(-child.pid!, 'SIGKILL')
    } catch {
      /* already stopped */
    }
  }
  for (const dir of directories.splice(0)) {
    await rm(dir, { recursive: true, force: true })
    await rm(`${dir}.json`, { force: true })
  }
})
it('stops the persisted replacement backend and leaves neighboring run processes and state intact', async () => {
  const [oldBackend, replacement, frontend, neighborBackend, neighborFrontend] = await Promise.all(
    Array.from({ length: 5 }, fixture),
  )
  const current = await stateFor(replacement, frontend)
  const neighbor = await stateFor(neighborBackend, neighborFrontend)
  const exited = new Promise<void>((resolve) => oldBackend.once('exit', () => resolve()))
  oldBackend.kill('SIGTERM')
  await exited
  ledger()
  const stop = vi.fn(async () => {})
  ;(globalThis as Record<string, unknown>).__acceptanceState = {
    ...current,
    backend: oldBackend,
    frontend,
    tutor: { stop },
  }
  await globalTeardown()
  expect(alive(replacement)).toBe(false)
  expect(alive(frontend)).toBe(false)
  expect(alive(neighborBackend)).toBe(true)
  expect(alive(neighborFrontend)).toBe(true)
  expect(JSON.parse(await readFile(neighbor.stateFile, 'utf8')).backendPid).toBe(
    neighborBackend.pid,
  )
  expect(stop).toHaveBeenCalledOnce()
})
it('fallback reads only this run state and still fails on unconsumed backend failures', async () => {
  const [backend, frontend, neighborBackend, neighborFrontend] = await Promise.all(
    Array.from({ length: 4 }, fixture),
  )
  const current = await stateFor(backend, frontend)
  const neighbor = await stateFor(neighborBackend, neighborFrontend)
  process.env.ACCEPTANCE_STATE_FILE = current.stateFile
  ledger(1)
  await expect(globalTeardown()).rejects.toThrow('unconsumed backend failure')
  expect(alive(backend)).toBe(false)
  expect(alive(frontend)).toBe(false)
  expect(alive(neighborBackend)).toBe(true)
  expect(alive(neighborFrontend)).toBe(true)
  expect(await readFile(neighbor.stateFile, 'utf8')).toContain(String(neighborBackend.pid))
})

it('fails closed when a persisted process identity cannot be verified', async () => {
  const [backend, frontend] = await Promise.all(Array.from({ length: 2 }, fixture))
  const current = await stateFor(backend, frontend)
  const state = JSON.parse(await readFile(current.stateFile, 'utf8'))
  state.frontendStartToken = null
  await writeFile(current.stateFile, JSON.stringify(state))
  process.env.ACCEPTANCE_STATE_FILE = current.stateFile
  ledger()
  await expect(globalTeardown()).rejects.toThrow('refusing to signal an unowned process')
  expect(alive(frontend)).toBe(true)
  expect(alive(backend)).toBe(true)
})

it('does not sweep fixture-shaped neighboring processes without current-run ownership', async () => {
  const neighbor = await fixture()
  delete process.env.ACCEPTANCE_STATE_FILE
  ledger()
  await globalTeardown()
  expect(alive(neighbor)).toBe(true)
})
