/**
 * Acceptance global setup -- starts the full production-shaped stack.
 *
 * 1. Resolves ports from env vars (ACCEPTANCE_BACKEND_PORT, etc.) or defaults.
 * 2. Creates an isolated temporary data directory.
 * 3. Starts the fake tutor fixture on the tutor port.
 * 4. Starts the real FastAPI backend (with deterministic embedding fixtures).
 * 5. Waits for the backend health endpoint.
 * 6. Configures the tutor endpoint + API key through PUT /api/settings.
 * 7. Builds the static frontend with the acceptance backend base URL and starts Vite preview.
 * 8. Waits for the frontend to respond.
 * 9. Writes a state file so global-teardown can stop everything.
 *
 * Partial-failure cleanup: if any step fails after starting a component,
 * all previously-started components are stopped before re-throwing.
 *
 * State file: uses a unique run identifier so parallel suites sharing a
 * project root do not collide.
 *
 * Ports are configurable via environment variables so parallel CI jobs or
 * local runs can avoid conflicts:
 *   ACCEPTANCE_BACKEND_PORT  (default 8000)
 *   ACCEPTANCE_FRONTEND_PORT (default 3000)
 *   ACCEPTANCE_TUTOR_PORT    (default 18900)
 */

import { type ChildProcess, execSync, spawn } from 'node:child_process'
import { mkdtemp, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

import { TutorFixture } from './tutor-fixture'

const PROJECT_ROOT = resolve(__dirname, '..', '..', '..')
const RUN_ID = `${process.pid}-${Date.now()}`
const STATE_FILE = join(PROJECT_ROOT, `.acceptance-state-${RUN_ID}.json`)

const READY_POLL_MS = 500
const BACKEND_TIMEOUT_MS = 60_000
const FRONTEND_TIMEOUT_MS = 120_000

function resolvePort(envVar: string, fallback: number): number {
  const v = process.env[envVar]
  return v ? Number(v) : fallback
}

export default async function globalSetup() {
  const BACKEND_PORT = resolvePort('ACCEPTANCE_BACKEND_PORT', 8000)
  const FRONTEND_PORT = resolvePort('ACCEPTANCE_FRONTEND_PORT', 3000)
  const TUTOR_PORT = resolvePort('ACCEPTANCE_TUTOR_PORT', 18_900)

  process.env.ACCEPTANCE_STATE_FILE = STATE_FILE

  // Publish resolved ports so workers and the config can read them.
  process.env.ACCEPTANCE_BACKEND_PORT = String(BACKEND_PORT)
  process.env.ACCEPTANCE_FRONTEND_PORT = String(FRONTEND_PORT)
  process.env.ACCEPTANCE_TUTOR_PORT = String(TUTOR_PORT)

  // ── 0. Guard against port conflicts ──────────────────────────────
  for (const [label, port] of [
    ['backend', BACKEND_PORT],
    ['frontend', FRONTEND_PORT],
    ['tutor', TUTOR_PORT],
  ] as const) {
    if (await isPortInUse(port)) {
      throw new Error(
        `Port ${port} (${label}) is already in use. Stop any running Lyra ` +
          `servers or set ACCEPTANCE_${label.toUpperCase()}_PORT to an open port.`,
      )
    }
  }

  // Track started components for partial-failure cleanup
  let tutor: TutorFixture | null = null
  let backend: ChildProcess | null = null
  let frontend: ChildProcess | null = null
  let dataDir: string | null = null

  async function cleanupOnFailure() {
    console.error('  Setup failed, cleaning up started components...')
    if (frontend) await killChild(frontend, 'frontend')
    if (backend) await killChild(backend, 'backend')
    if (tutor) {
      try {
        await tutor.stop()
      } catch {
        /* already gone */
      }
    }
    if (dataDir && dataDir.includes('lyra-acceptance')) {
      const { rm } = await import('node:fs/promises')
      try {
        await rm(dataDir, { recursive: true, force: true })
        console.log(`  Removed data dir ${dataDir}`)
      } catch {
        console.warn(`  Could not remove data dir ${dataDir}`)
      }
    }
  }

  try {
    // ── 1. Isolated data directory ───────────────────────────────────
    dataDir = await mkdtemp(join(tmpdir(), 'lyra-acceptance-'))

    // ── 2. Fake tutor fixture ────────────────────────────────────────
    tutor = new TutorFixture(TUTOR_PORT)
    await tutor.start()
    console.log(`  Tutor fixture listening on ${tutor.baseUrl}`)

    // ── 3. Real FastAPI backend ──────────────────────────────────────
    const backendEnv: Record<string, string> = {
      ...stripUndefined(process.env),
      LYRA_DATA_DIR: dataDir,
      LYRA_HOST: '127.0.0.1',
      LYRA_PORT: String(BACKEND_PORT),
      LYRA_BROWSER_ORIGINS: `http://127.0.0.1:${FRONTEND_PORT}`,
      PYTHONDONTWRITEBYTECODE: '1',
    }

    // `detached: true` makes the child a new session/process-group leader (pgid == pid).
    // Teardown then signals the whole group (-pid), which reclaims the real uvicorn
    // python grandchild that outlives the `uv` wrapper. Without this, SIGTERM to the
    // wrapper alone orphans the backend (and any helper it spawned) -- exactly the
    // process leakage the zero-orphan gate forbids.
    backend = spawn(
      'uv',
      [
        'run',
        'python',
        '-m',
        'uvicorn',
        'acceptance.backend_harness:app',
        '--host',
        '127.0.0.1',
        '--port',
        String(BACKEND_PORT),
        '--log-level',
        'warning',
      ],
      {
        cwd: PROJECT_ROOT,
        env: backendEnv as NodeJS.ProcessEnv,
        stdio: ['ignore', 'pipe', 'pipe'],
        detached: true,
      },
    ) as ChildProcess

    collectOutput(backend, 'backend')
    console.log(`  Backend starting (pid ${backend.pid}) with data dir ${dataDir}`)

    // ── 4. Wait for backend health ───────────────────────────────────
    await waitForUrl(
      `http://127.0.0.1:${BACKEND_PORT}/api/health/ready`,
      BACKEND_TIMEOUT_MS,
      'Backend',
    )
    console.log('  Backend ready')

    // ── 5. Configure tutor endpoint ──────────────────────────────────
    const settingsRes = await fetch(`http://127.0.0.1:${BACKEND_PORT}/api/settings`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        'X-Lyra-Client': 'acceptance-setup',
      },
      body: JSON.stringify({
        endpoint_url: tutor.baseUrl,
        api_key: 'test-acceptance-key',
        model: 'test-model',
        context_window: 8192,
        remote_ack: true,
      }),
    })
    if (!settingsRes.ok) {
      const text = await settingsRes.text()
      throw new Error(`Failed to configure tutor endpoint: ${settingsRes.status} ${text}`)
    }
    console.log('  Tutor endpoint configured')

    // ── 6. Production frontend ───────────────────────────────────────
    execSync('./node_modules/.bin/vite build', {
      cwd: join(PROJECT_ROOT, 'frontend'),
      env: {
        ...stripUndefined(process.env),
        VITE_API_BASE: `http://127.0.0.1:${BACKEND_PORT}`,
      } as NodeJS.ProcessEnv,
      stdio: 'pipe',
    })

    // Same process-group rationale as the backend: Vite preview runs as a wrapper
    // process with children that should be reclaimed together in teardown.
    frontend = spawn(
      './node_modules/.bin/vite',
      ['preview', '--host', '127.0.0.1', '--port', String(FRONTEND_PORT), '--strictPort'],
      {
        cwd: join(PROJECT_ROOT, 'frontend'),
        env: stripUndefined(process.env) as NodeJS.ProcessEnv,
        stdio: ['ignore', 'pipe', 'pipe'],
        detached: true,
      },
    ) as ChildProcess

    collectOutput(frontend, 'frontend')
    console.log(`  Frontend starting (pid ${frontend.pid})`)

    // ── 7. Wait for frontend ─────────────────────────────────────────
    await waitForUrl(`http://127.0.0.1:${FRONTEND_PORT}`, FRONTEND_TIMEOUT_MS, 'Frontend')
    console.log('  Frontend ready')
  } catch (err) {
    await cleanupOnFailure()
    throw err
  }

  // ── 8. Persist state for teardown ────────────────────────────────
  // Store process birth tokens (start times) for PID identity verification
  // in teardown -- prevents killing recycled PIDs.
  const backendStartToken = backend.pid ? processStartToken(backend.pid) : null
  const frontendStartToken = frontend.pid ? processStartToken(frontend.pid) : null

  const statePayload = {
    runId: RUN_ID,
    dataDir,
    backendPid: backend.pid,
    frontendPid: frontend.pid,
    backendPort: BACKEND_PORT,
    frontendPort: FRONTEND_PORT,
    tutorPort: TUTOR_PORT,
    backendStartToken,
    frontendStartToken,
    startedAt: Date.now(),
  }

  await writeFile(STATE_FILE, JSON.stringify(statePayload))

  // Publish the state file path so teardown and tests can find it
  process.env.ACCEPTANCE_STATE_FILE = STATE_FILE

  // Keep references alive so Node doesn't GC the child processes
  ;(globalThis as Record<string, unknown>).__acceptanceState = {
    tutor,
    backend,
    frontend,
    dataDir,
    stateFile: STATE_FILE,
  }

  console.log('  Acceptance stack ready\n')
}

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

async function waitForUrl(url: string, timeoutMs: number, label: string) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url, {
        signal: AbortSignal.timeout(2000),
      })
      if (res.ok) return
    } catch {
      // not ready yet
    }
    await sleep(READY_POLL_MS)
  }
  throw new Error(`${label} did not become ready within ${timeoutMs}ms at ${url}`)
}

async function isPortInUse(port: number): Promise<boolean> {
  try {
    const res = await fetch(`http://127.0.0.1:${port}`, {
      signal: AbortSignal.timeout(500),
    })
    void res.body?.cancel()
    return true
  } catch {
    return false
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}

async function killChild(proc: ChildProcess, label: string): Promise<void> {
  if (!proc.pid) return
  if (proc.exitCode !== null || proc.signalCode !== null) return
  const pid = proc.pid

  const exitPromise = new Promise<void>((resolve) => {
    proc.on('exit', () => resolve())
    proc.on('error', () => resolve())
  })

  // The child was spawned detached, so its pid is a process-group leader (pgid == pid).
  // Signal the whole group (-pid): that reclaims the real uvicorn python / next worker
  // processes that outlive the `uv` / `pnpm exec` wrapper. Signalling the wrapper alone
  // orphans them -- exactly the leakage the zero-orphan gate forbids. If the group is
  // already gone, fall back to signalling the wrapper itself.
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

  if (!signalGroup('SIGTERM')) return
  console.log(`  Sent SIGTERM to ${label} group (pid ${pid})`)

  const termResult = await Promise.race([
    exitPromise.then(() => 'exited' as const),
    sleep(5_000).then(() => 'timeout' as const),
  ])

  if (termResult === 'timeout') {
    console.log(`  ${label} did not exit after SIGTERM, sending SIGKILL`)
    signalGroup('SIGKILL')
    await Promise.race([exitPromise, sleep(3_000)])
  }

  if (proc.exitCode === null && proc.signalCode === null) {
    throw new Error(
      `Setup cleanup: ${label} (pid ${pid}) could not be proven dead after SIGTERM + SIGKILL`,
    )
  }
  console.log(`  ${label} stopped`)
}

function stripUndefined(env: NodeJS.ProcessEnv): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(env)) {
    if (v !== undefined) out[k] = v
  }
  return out
}

function collectOutput(proc: ChildProcess, label: string) {
  const sink = (stream: NodeJS.ReadableStream | null, level: string) => {
    stream?.on('data', (chunk: Buffer) => {
      const text = chunk.toString().trim()
      if (text) {
        for (const line of text.split('\n')) {
          console.log(`  [${label}:${level}] ${line}`)
        }
      }
    })
  }
  sink(proc.stdout, 'out')
  sink(proc.stderr, 'err')

  proc.on('exit', (code, signal) => {
    console.log(`  [${label}] exited code=${code} signal=${signal}`)
  })
}

/**
 * Return a birth token for a running process -- the kernel-level start time
 * that survives across PID checks. On macOS, uses `ps -p PID -o lstart=`.
 * Returns null if the process is not found.
 */
function processStartToken(pid: number): string | null {
  try {
    return execSync(`ps -p ${pid} -o lstart=`, { encoding: 'utf-8', timeout: 2000 }).trim()
  } catch {
    return null
  }
}
