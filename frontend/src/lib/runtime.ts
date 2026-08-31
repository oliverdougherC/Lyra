export type RuntimeConfig = {
  apiBase: string
  sessionHeader: string | null
  source: 'vite-env' | 'tauri' | 'browser-fallback'
}

type BootstrapPayload = {
  apiBase: string
  sessionHeader: string | null
}

type TauriCore = {
  invoke?: (command: string, args?: Record<string, unknown>) => Promise<unknown>
}

declare global {
  interface Window {
    __LYRA_BOOTSTRAP__?: Partial<
      Record<'apiBase' | 'api_base' | 'sessionHeader' | 'session_id', string>
    >
    __TAURI__?: { core?: TauriCore }
    __TAURI_INTERNALS__?: TauriCore
  }
}

let runtimeConfig: RuntimeConfig | null = null
let runtimePromise: Promise<RuntimeConfig> | null = null

function readBootstrapPayload(
  payload: Partial<Record<'apiBase' | 'api_base' | 'sessionHeader' | 'session_id', string>>,
): BootstrapPayload | null {
  const apiBase = payload.apiBase ?? payload.api_base
  if (!apiBase || typeof apiBase !== 'string') return null
  const sessionHeader = payload.sessionHeader ?? payload.session_id ?? null
  return { apiBase, sessionHeader }
}

function browserFallback(): RuntimeConfig {
  return {
    apiBase: import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000',
    sessionHeader: null,
    source: import.meta.env.VITE_API_BASE ? 'vite-env' : 'browser-fallback',
  }
}

function tauriInvoke() {
  return window.__TAURI__?.core?.invoke ?? window.__TAURI_INTERNALS__?.invoke
}

async function readTauriBootstrap(command = 'desktop_bootstrap'): Promise<RuntimeConfig | null> {
  const invoke = tauriInvoke()

  if (!invoke) return null

  const raw = await invoke(command)
  if (!raw || typeof raw !== 'object') {
    throw new Error('desktop_bootstrap returned an unexpected payload.')
  }

  const payload = readBootstrapPayload(
    raw as Partial<Record<'apiBase' | 'api_base' | 'sessionHeader' | 'session_id', string>>,
  )
  if (!payload) {
    throw new Error('desktop_bootstrap did not provide an API base.')
  }

  return {
    apiBase: payload.apiBase,
    sessionHeader: payload.sessionHeader,
    source: 'tauri',
  }
}

export async function recoverDesktopBackend(): Promise<boolean> {
  if (runtimeConfig?.source !== 'tauri' || !tauriInvoke()) return false
  const restarted = await readTauriBootstrap('desktop_bootstrap')
  if (!restarted) return false
  runtimeConfig = restarted
  runtimePromise = Promise.resolve(restarted)
  return true
}

export async function initializeRuntimeConfig(): Promise<RuntimeConfig> {
  if (runtimeConfig) return runtimeConfig
  if (runtimePromise) return runtimePromise

  runtimePromise = (async () => {
    const injected = window.__LYRA_BOOTSTRAP__
    const injectedPayload = injected ? readBootstrapPayload(injected) : null
    if (injectedPayload) {
      runtimeConfig = {
        apiBase: injectedPayload.apiBase,
        sessionHeader: injectedPayload.sessionHeader,
        source: 'vite-env',
      }
      return runtimeConfig
    }

    if (import.meta.env.DEV || import.meta.env.VITE_API_BASE) {
      runtimeConfig = browserFallback()
      return runtimeConfig
    }

    // In the packaged app a failed bootstrap is a native startup failure, not a reason
    // to guess the old development port and trust an unrelated listener.
    const tauriConfig = await readTauriBootstrap()
    runtimeConfig = tauriConfig ?? browserFallback()
    return runtimeConfig
  })()

  return runtimePromise
}

export async function getRuntimeConfig(): Promise<RuntimeConfig> {
  return runtimeConfig ?? initializeRuntimeConfig()
}

export function getImmediateRuntimeConfig(): RuntimeConfig | null {
  if (runtimeConfig) return runtimeConfig

  const injected = typeof window === 'undefined' ? undefined : window.__LYRA_BOOTSTRAP__
  const injectedPayload = injected ? readBootstrapPayload(injected) : null
  if (injectedPayload) {
    return {
      apiBase: injectedPayload.apiBase,
      sessionHeader: injectedPayload.sessionHeader,
      source: 'vite-env',
    }
  }

  if (typeof window !== 'undefined' && (import.meta.env.DEV || import.meta.env.VITE_API_BASE)) {
    return browserFallback()
  }

  return null
}
