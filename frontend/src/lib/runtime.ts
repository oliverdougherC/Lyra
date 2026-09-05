import { downloadBlob } from './external-links'

export type RuntimeConfig = {
  apiBase: string
  sessionHeader: string | null
  source: 'vite-env' | 'tauri' | 'browser-fallback'
}

export type DesktopImportSelection = {
  selectionToken: string
  label: string
}

type BootstrapPayload = {
  apiBase: string
  sessionHeader: string | null
}

type TauriBootstrapPayload = {
  protocolVersion: number
  apiBase: string
  sessionHeaderName: string
  sessionSecret: string
}

const BOOTSTRAP_PROTOCOL_VERSION = 1
const SESSION_HEADER_NAME = 'X-Lyra-Session'
const SESSION_SECRET_PATTERN = /^[0-9a-f]{64}$/
const TAURI_BOOTSTRAP_KEYS = [
  'apiBase',
  'protocolVersion',
  'sessionHeaderName',
  'sessionSecret',
] as const

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

function parseTauriBootstrap(raw: unknown): TauriBootstrapPayload {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('The native desktop bootstrap returned an invalid payload.')
  }
  const payload = raw as Record<string, unknown>
  const keys = Object.keys(payload).sort()
  if (
    keys.length !== TAURI_BOOTSTRAP_KEYS.length ||
    !TAURI_BOOTSTRAP_KEYS.every((key, index) => keys[index] === key)
  ) {
    throw new Error('The native desktop bootstrap returned an unsupported payload.')
  }
  if (payload.protocolVersion !== BOOTSTRAP_PROTOCOL_VERSION) {
    throw new Error('The native desktop bootstrap protocol is unsupported.')
  }
  if (payload.sessionHeaderName !== SESSION_HEADER_NAME) {
    throw new Error('The native desktop bootstrap session header is unsupported.')
  }
  if (
    typeof payload.sessionSecret !== 'string' ||
    !SESSION_SECRET_PATTERN.test(payload.sessionSecret)
  ) {
    throw new Error('The native desktop bootstrap session secret is invalid.')
  }
  if (typeof payload.apiBase !== 'string') {
    throw new Error('The native desktop bootstrap API address is invalid.')
  }
  let apiUrl: URL
  try {
    apiUrl = new URL(payload.apiBase)
  } catch {
    throw new Error('The native desktop bootstrap API address is invalid.')
  }
  if (
    apiUrl.protocol !== 'http:' ||
    apiUrl.hostname !== '127.0.0.1' ||
    !apiUrl.port ||
    apiUrl.username ||
    apiUrl.password ||
    apiUrl.pathname !== '/' ||
    apiUrl.search ||
    apiUrl.hash
  ) {
    throw new Error('The native desktop bootstrap API address is invalid.')
  }
  return payload as TauriBootstrapPayload
}

async function readTauriBootstrap(command = 'desktop_bootstrap'): Promise<RuntimeConfig | null> {
  const invoke = tauriInvoke()

  if (!invoke) return null

  const raw = await invoke(command)
  const payload = parseTauriBootstrap(raw)

  return {
    apiBase: payload.apiBase,
    sessionHeader: payload.sessionSecret,
    source: 'tauri',
  }
}

function adoptRuntimeConfig(config: RuntimeConfig): RuntimeConfig {
  runtimeConfig = config
  runtimePromise = Promise.resolve(config)
  return config
}

export async function recoverDesktopBackend(): Promise<boolean> {
  if (!tauriInvoke()) return false
  const restarted = await readTauriBootstrap('retry_backend')
  if (!restarted) return false
  adoptRuntimeConfig(restarted)
  return true
}

export async function pickDesktopImportDirectory(): Promise<DesktopImportSelection | null> {
  const invoke = tauriInvoke()
  if (!invoke) return null
  const raw = await invoke('pick_import_directory')
  if (raw === null) return null
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('The native folder picker returned an invalid selection.')
  }
  const selection = raw as Record<string, unknown>
  const keys = Object.keys(selection).sort()
  if (keys.length !== 2 || keys[0] !== 'label' || keys[1] !== 'selectionToken') {
    throw new Error('The native folder picker returned an unsupported selection.')
  }
  if (
    typeof selection.selectionToken !== 'string' ||
    !SESSION_SECRET_PATTERN.test(selection.selectionToken) ||
    typeof selection.label !== 'string' ||
    !selection.label.trim() ||
    selection.label.length > 255
  ) {
    throw new Error('The native folder picker returned an invalid selection.')
  }
  return {
    selectionToken: selection.selectionToken,
    label: selection.label,
  }
}

/**
 * Open the native folder picker for the contextual agent's workspace attach. Returns the
 * chosen path, or null when the picker is unavailable (browser build) or the student
 * cancelled. Desktop-only by design: the picker is the normal way a student names a
 * folder; a raw path entry remains as a fallback for non-desktop environments.
 */
export async function pickDesktopWorkspaceDirectory(): Promise<string | null> {
  const invoke = tauriInvoke()
  if (!invoke) return null
  const raw = await invoke('pick_workspace_directory')
  return typeof raw === 'string' && raw.trim() ? raw : null
}

/** True when the desktop shell is present and can open a native folder picker. */
export function desktopFolderPickerAvailable(): boolean {
  return tauriInvoke() !== undefined
}

export async function publishDesktopImport(): Promise<boolean> {
  if (!tauriInvoke()) return false
  try {
    const restarted = await readTauriBootstrap('publish_desktop_import')
    if (!restarted) return false
    adoptRuntimeConfig(restarted)
    return true
  } catch (error) {
    const recovered = await readTauriBootstrap('desktop_bootstrap').catch(() => null)
    if (recovered) adoptRuntimeConfig(recovered)
    throw error
  }
}

export async function initializeRuntimeConfig(): Promise<RuntimeConfig> {
  if (runtimeConfig) return runtimeConfig
  if (runtimePromise) return runtimePromise

  runtimePromise = (async () => {
    const browserDevelopment = import.meta.env.DEV || Boolean(import.meta.env.VITE_API_BASE)
    if (browserDevelopment) {
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
      runtimeConfig = browserFallback()
      return runtimeConfig
    }

    // In the packaged app a failed bootstrap is a native startup failure, not a reason
    // to guess the old development port and trust an unrelated listener.
    const tauriConfig = await readTauriBootstrap()
    if (!tauriConfig) {
      throw new Error('The native desktop bootstrap is unavailable.')
    }
    runtimeConfig = tauriConfig
    return tauriConfig
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

/** Save authenticated bytes through the native dialog, without exposing its selected path. */
export async function saveOriginalDocument(
  blob: Blob,
  filename: string,
): Promise<'saved' | 'cancelled' | 'downloaded'> {
  const safeName = filename.replace(/[^A-Za-z0-9._-]/g, '_').replace(/^\.+/, '') || 'document'
  const invoke = tauriInvoke()
  if (invoke) {
    const saved = await invoke('save_original_document', {
      filename: safeName,
      bytes: Array.from(new Uint8Array(await blob.arrayBuffer())),
    })
    return saved ? 'saved' : 'cancelled'
  }
  downloadBlob(blob, safeName)
  return 'downloaded'
}
