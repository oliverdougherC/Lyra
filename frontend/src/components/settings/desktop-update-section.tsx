import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { assertUpdateSafe } from '@/lib/update-safety'

type UpdateStatus = {
  currentVersion: string
  currentBuild: string
  recoveryAvailable: boolean
  channel: string
  phase: string
  checkedAt: number | null
  version: string | null
  notes: string | null
  downloaded: number
  total: number | null
  error: string | null
}
function invoke(command: string): Promise<unknown> {
  const native = window.__TAURI__?.core?.invoke ?? window.__TAURI_INTERNALS__?.invoke
  if (!native) return Promise.reject(new Error('Updates are available in the desktop app.'))
  return native(command)
}
export function DesktopUpdateSection({ unsavedSettings }: { unsavedSettings: boolean }) {
  const queryClient = useQueryClient()
  const [status, setStatus] = useState<UpdateStatus | null>(null)
  const [operation, setOperation] = useState(false)
  const [installing, setInstalling] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const native = !!(window.__TAURI__?.core?.invoke ?? window.__TAURI_INTERNALS__?.invoke)
  async function refresh() {
    setStatus((await invoke('desktop_update_status')) as UpdateStatus)
  }
  useEffect(() => {
    // This reads local state only. Checking the network always requires the button.
    if (native)
      void invoke('desktop_update_status')
        .then((value) => setStatus(value as UpdateStatus))
        .catch((failure) => setError(failure instanceof Error ? failure.message : String(failure)))
  }, [native])
  useEffect(() => {
    if (!operation) return
    const timer = window.setInterval(() => {
      void refresh().catch(() => undefined)
    }, 300)
    return () => window.clearInterval(timer)
  }, [operation])
  if (!native) return null
  async function run(command: string) {
    setError(null)
    if (command === 'install_desktop_update') {
      try {
        if (unsavedSettings || queryClient.isMutating()) {
          throw new Error('Finish saving your settings and changes before installing the update.')
        }
        assertUpdateSafe()
      } catch (failure) {
        setError(failure instanceof Error ? failure.message : String(failure))
        return
      }
      setInstalling(true)
    }
    setOperation(true)
    try {
      await invoke(command)
      if (command === 'install_desktop_update') {
        setStatus((current) => (current ? { ...current, phase: 'restart' } : current))
        setInstalling(false)
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure))
      setInstalling(false)
    } finally {
      await refresh().catch(() =>
        setError('Could not refresh update status. If an update was installed, restart Lyra.'),
      )
      setOperation(false)
    }
  }
  const restarting = status?.phase === 'restart'
  return (
    <section className="space-y-3" aria-labelledby="desktop-update-title">
      <h2 id="desktop-update-title" className="font-display text-xl">
        Application updates
      </h2>
      <p className="text-sm text-muted-foreground">
        Lyra {status?.currentVersion ?? '…'} (build {status?.currentBuild ?? '…'}) ·{' '}
        {status?.channel ?? 'beta'} channel. Updates are checked and downloaded only when you ask.
      </p>
      <p className="text-sm" aria-live="polite">
        {status?.checkedAt
          ? `Last checked ${new Date(status.checkedAt * 1000).toLocaleString()}.`
          : 'Not checked.'}
        {status?.phase === 'up-to-date' && ' You have the latest version on this channel.'}
        {status?.version && ` Version ${status.version} is available.`}
        {status?.phase === 'checking' && ' Checking…'}
        {status?.phase === 'verifying' && ' Verifying the downloaded application…'}
        {status?.phase === 'downloading' &&
          ` Downloading ${Math.floor(status.downloaded / 1024 / 1024)} of ${Math.ceil((status.total ?? 0) / 1024 / 1024)} MB…`}
        {status?.phase === 'ready' && ' Download verified and ready to install.'}
      </p>
      {status?.notes && (
        <details className="text-sm">
          <summary>Release notes</summary>
          <p className="whitespace-pre-wrap">{status.notes}</p>
        </details>
      )}
      {(error || status?.error) && (
        <p role="alert" className="text-sm text-danger-text">
          {error ?? status?.error}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          variant="outline"
          disabled={operation || restarting}
          onClick={() => void run('check_desktop_update')}
        >
          Check for updates
        </Button>
        {status?.phase === 'available' && (
          <Button disabled={operation} onClick={() => void run('download_desktop_update')}>
            Download update
          </Button>
        )}
        {status?.phase === 'downloading' && (
          <Button variant="outline" onClick={() => void invoke('cancel_desktop_update')}>
            Cancel download
          </Button>
        )}
        {status?.phase === 'ready' && (
          <Button disabled={operation} onClick={() => void run('install_desktop_update')}>
            Install update
          </Button>
        )}
      </div>
      {status?.phase === 'ready' && (
        <p className="text-sm text-muted-foreground">
          Save your changes first. Active work will be interrupted; durable jobs recover after
          relaunch. Your documents, writing, settings, keys, and downloaded helpers stay on this
          Mac. If the new app migrates data, it keeps a verified database backup; returning to an
          older app then requires restoring that backup separately.
        </p>
      )}
      {status?.recoveryAvailable && (
        <div className="space-y-2">
          <Button
            variant="outline"
            disabled={operation}
            onClick={() => void run('desktop_update_recovery')}
          >
            Show previous Lyra app
          </Button>
          <p className="text-sm text-muted-foreground">
            For recovery, quit Lyra and open the retained app in Finder. If data was migrated,
            restore a compatible backup separately first. Your current data is never downgraded.
          </p>
        </div>
      )}
      <Dialog open={installing || restarting}>
        <DialogContent
          showCloseButton={false}
          onEscapeKeyDown={(event) => event.preventDefault()}
          onInteractOutside={(event) => event.preventDefault()}
        >
          <DialogTitle>{restarting ? 'Update installed' : 'Installing update'}</DialogTitle>
          <DialogDescription>
            {restarting
              ? 'Restart Lyra to use the new version. Your student data is retained.'
              : 'Lyra is stopping its own active work and replacing the application. Keep the app open.'}
          </DialogDescription>
          {error && <p role="alert">{error}</p>}
          {restarting && (
            <Button onClick={() => void run('restart_desktop_update')}>Restart Lyra</Button>
          )}
        </DialogContent>
      </Dialog>
    </section>
  )
}
