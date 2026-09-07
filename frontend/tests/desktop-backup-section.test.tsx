import { RouterProvider } from '@/router/hooks'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { getRuntimeConfig } from '@/lib/runtime'
import { DesktopBackupSection } from '@/components/settings/desktop-backup-section'

function setup(unsavedSettings = false) {
  const invoke = vi.fn().mockResolvedValue({ status: 'cancelled', label: '' })
  Object.assign(window, { __TAURI_INTERNALS__: { invoke } })
  render(
    <QueryClientProvider client={new QueryClient()}>
      <RouterProvider>
        <DesktopBackupSection unsavedSettings={unsavedSettings} />
      </RouterProvider>
    </QueryClientProvider>,
  )
  fireEvent.click(screen.getByRole('button', { name: /Backup and restore/ }))
  return invoke
}
afterEach(() => {
  delete window.__TAURI_INTERNALS__
})
describe('desktop backups', () => {
  it('never starts backup without an explicit request and preserves native cancel', async () => {
    const invoke = setup()
    expect(invoke).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Save backup' }))
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('desktop_backup_create'))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })
  it('refuses to stop the backend while settings have unsaved edits', async () => {
    const invoke = setup(true)
    fireEvent.click(screen.getByRole('button', { name: 'Restore backup' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Finish saving')
    expect(invoke).not.toHaveBeenCalled()
  })
  it('surfaces native failure without claiming a successful restore', async () => {
    const invoke = setup()
    invoke.mockRejectedValueOnce(new Error('Archive failed verification. Current data retained.'))
    invoke.mockResolvedValueOnce({
      protocolVersion: 1,
      apiBase: 'http://127.0.0.1:48123',
      sessionHeaderName: 'X-Lyra-Session',
      sessionSecret: 'a'.repeat(64),
    })
    fireEvent.click(screen.getByRole('button', { name: 'Restore backup' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Archive failed verification')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect((await getRuntimeConfig()).apiBase).toBe('http://127.0.0.1:48123')
  })
})
