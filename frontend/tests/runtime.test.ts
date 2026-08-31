import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const originalBootstrap = window.__LYRA_BOOTSTRAP__

async function loadRuntime() {
  vi.resetModules()
  return import('@/lib/runtime')
}

beforeEach(() => {
  delete window.__LYRA_BOOTSTRAP__
  delete window.__TAURI__
  delete window.__TAURI_INTERNALS__
  vi.stubEnv('DEV', false)
  vi.stubEnv('VITE_API_BASE', '')
})

afterEach(() => {
  window.__LYRA_BOOTSTRAP__ = originalBootstrap
  vi.unstubAllEnvs()
})

describe('desktop runtime bootstrap', () => {
  it('fails closed when packaged Tauri IPC is unavailable', async () => {
    const { initializeRuntimeConfig } = await loadRuntime()

    await expect(initializeRuntimeConfig()).rejects.toThrow(
      'The native desktop bootstrap is unavailable.',
    )
  })

  it('keeps the fixed loopback fallback behind explicit browser development mode', async () => {
    vi.stubEnv('DEV', true)
    const { initializeRuntimeConfig } = await loadRuntime()

    await expect(initializeRuntimeConfig()).resolves.toEqual({
      apiBase: 'http://127.0.0.1:8000',
      sessionHeader: null,
      source: 'browser-fallback',
    })
  })

  it('uses the trusted retry command even when initial bootstrap never succeeded', async () => {
    const invoke = vi.fn(async (command: string) => {
      expect(command).toBe('retry_backend')
      return {
        protocolVersion: 1,
        apiBase: 'http://127.0.0.1:43123',
        sessionHeaderName: 'X-Lyra-Session',
        sessionSecret: 'a'.repeat(64),
      }
    })
    window.__TAURI_INTERNALS__ = { invoke }
    const { getRuntimeConfig, recoverDesktopBackend } = await loadRuntime()

    await expect(recoverDesktopBackend()).resolves.toBe(true)
    await expect(getRuntimeConfig()).resolves.toMatchObject({
      apiBase: 'http://127.0.0.1:43123',
      sessionHeader: 'a'.repeat(64),
      source: 'tauri',
    })
    expect(invoke).toHaveBeenCalledTimes(1)
  })

  it.each([
    { protocolVersion: 2 },
    { apiBase: 'http://127.0.0.1:8000/path' },
    { apiBase: 'http://localhost:8000' },
    { apiBase: 'http://127.0.0.1:8000', extra: true },
    { sessionHeaderName: 'Authorization' },
    { sessionSecret: 'not-a-secret' },
  ])('rejects malformed or security-incompatible native payloads: %o', async (override) => {
    window.__TAURI_INTERNALS__ = {
      invoke: async () => ({
        protocolVersion: 1,
        apiBase: 'http://127.0.0.1:43123',
        sessionHeaderName: 'X-Lyra-Session',
        sessionSecret: 'a'.repeat(64),
        ...override,
      }),
    }
    const { initializeRuntimeConfig } = await loadRuntime()

    await expect(initializeRuntimeConfig()).rejects.toThrow(/native desktop bootstrap/i)
  })

  it('returns only an opaque native import selection', async () => {
    window.__TAURI_INTERNALS__ = {
      invoke: async (command) => {
        expect(command).toBe('pick_import_directory')
        return { selectionToken: 'b'.repeat(64), label: 'Old Lyra' }
      },
    }
    const { pickDesktopImportDirectory } = await loadRuntime()

    await expect(pickDesktopImportDirectory()).resolves.toEqual({
      selectionToken: 'b'.repeat(64),
      label: 'Old Lyra',
    })
  })

  it('rejects a picker payload that exposes a filesystem path', async () => {
    window.__TAURI_INTERNALS__ = {
      invoke: async () => ({
        selectionToken: 'b'.repeat(64),
        label: 'Old Lyra',
        path: '/private/coursework',
      }),
    }
    const { pickDesktopImportDirectory } = await loadRuntime()

    await expect(pickDesktopImportDirectory()).rejects.toThrow(/unsupported selection/i)
  })

  it('adopts the fresh bootstrap returned after native import publication', async () => {
    const invoke = vi.fn(async (command: string) => {
      expect(command).toBe('publish_desktop_import')
      return {
        protocolVersion: 1,
        apiBase: 'http://127.0.0.1:43124',
        sessionHeaderName: 'X-Lyra-Session',
        sessionSecret: 'c'.repeat(64),
      }
    })
    window.__TAURI_INTERNALS__ = { invoke }
    const { getRuntimeConfig, publishDesktopImport } = await loadRuntime()

    await expect(publishDesktopImport()).resolves.toBe(true)
    await expect(getRuntimeConfig()).resolves.toMatchObject({
      apiBase: 'http://127.0.0.1:43124',
      sessionHeader: 'c'.repeat(64),
      source: 'tauri',
    })
  })
})
