import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { DesktopUpdateSection } from '@/components/settings/desktop-update-section'
import { assertUpdateSafe, reportUpdateSaveState } from '@/lib/update-safety'

const initial = {
  currentVersion: '0.2.0-beta.0',
  channel: 'beta',
  phase: 'not-checked',
  checkedAt: null,
  version: null,
  notes: null,
  downloaded: 0,
  total: null,
  error: null,
}
afterEach(() => {
  cleanup()
  delete window.__TAURI_INTERNALS__
})
function mount(unsavedSettings = false) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <DesktopUpdateSection unsavedSettings={unsavedSettings} />
    </QueryClientProvider>,
  )
}
describe('explicit desktop updates', () => {
  it('mount reads local version but never checks or downloads', async () => {
    const invoke = vi.fn().mockResolvedValue(initial)
    window.__TAURI_INTERNALS__ = { invoke }
    mount()
    await screen.findByText(/Not checked/)
    expect(invoke.mock.calls).toEqual([['desktop_update_status']])
    fireEvent.click(screen.getByRole('button', { name: 'Check for updates' }))
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('check_desktop_update'))
    expect(invoke).not.toHaveBeenCalledWith('download_desktop_update')
  })
  it('refuses installation while settings are dirty', async () => {
    const invoke = vi
      .fn()
      .mockResolvedValue({ ...initial, phase: 'ready', version: '0.2.0-beta.1' })
    window.__TAURI_INTERNALS__ = { invoke }
    mount(true)
    fireEvent.click(await screen.findByRole('button', { name: 'Install update' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Finish saving')
    expect(invoke).not.toHaveBeenCalledWith('install_desktop_update')
  })
  it('keeps explicit restart available if status refresh fails after installation', async () => {
    let reads = 0
    const invoke = vi.fn(async (command: string) => {
      if (command === 'desktop_update_status') {
        if (reads++ > 0) throw new Error('status unavailable')
        return { ...initial, phase: 'ready', version: '0.2.0-beta.1' }
      }
      return undefined
    })
    window.__TAURI_INTERNALS__ = { invoke }
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Install update' }))
    await screen.findByRole('button', { name: 'Restart Lyra' })
    expect(invoke).toHaveBeenCalledWith('install_desktop_update')
    expect(invoke).not.toHaveBeenCalledWith('restart_desktop_update')
  })
  it('reports local status errors without checking the network', async () => {
    const invoke = vi.fn().mockRejectedValue(new Error('local state unavailable'))
    window.__TAURI_INTERNALS__ = { invoke }
    mount()
    expect(await screen.findByRole('alert')).toHaveTextContent('local state unavailable')
    expect(invoke.mock.calls).toEqual([['desktop_update_status']])
  })
  it('holds an unmounted pending writer until its save is confirmed', () => {
    const owner = Symbol('unmounted-editor')
    reportUpdateSaveState(owner, false)
    expect(() => assertUpdateSafe()).toThrow('Finish saving')
    reportUpdateSaveState(owner, true)
    expect(() => assertUpdateSafe()).not.toThrow()
  })
})
