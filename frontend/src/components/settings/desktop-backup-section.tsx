'use client'

import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { SettingsDisclosure } from '@/components/settings/settings-disclosure'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { runDesktopBackup } from '@/lib/runtime'
import { assertUpdateSafe } from '@/lib/update-safety'

export function DesktopBackupSection({ unsavedSettings }: { unsavedSettings: boolean }) {
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const native = window.__TAURI__?.core?.invoke ?? window.__TAURI_INTERNALS__?.invoke
  if (!native) return null

  async function run(command: 'desktop_backup_create' | 'desktop_backup_restore') {
    setError(null)
    try {
      if (unsavedSettings || queryClient.isMutating()) {
        throw new Error('Finish saving your settings and changes before backing up or restoring.')
      }
      assertUpdateSafe('backing up or restoring')
      setBusy(true)
      const result = await runDesktopBackup(command)
      if (result.status !== 'cancelled') window.location.reload()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure))
    } finally {
      setBusy(false)
    }
  }

  return (
    <SettingsDisclosure
      title="Backup and restore"
      description="Save a private copy or restore an existing backup."
      attention={busy || !!error}
      anchors={['desktop-backup']}
    >
      <div className="space-y-3">
        <p className="text-text-secondary text-sm">
          Save your changes first. Lyra pauses its active work to create a verified archive of your
          classes, documents, writing, sources, study progress, and settings. Store it somewhere
          private. Keychain credentials are kept separately and may need to be configured on another
          Mac.
        </p>
        <div className="flex flex-wrap gap-2">
          <Button
            id="desktop-backup"
            variant="outline"
            disabled={busy}
            onClick={() => void run('desktop_backup_create')}
          >
            Save backup
          </Button>
          <Button
            variant="outline"
            disabled={busy}
            onClick={() => void run('desktop_backup_restore')}
          >
            Restore backup
          </Button>
        </div>
        <p className="text-text-secondary text-sm">
          Restore verifies the archive before switching profiles and retains your current data as a
          recovery copy. Lyra reloads when the operation finishes.
        </p>
        {error && (
          <p role="alert" className="text-danger-text text-sm">
            {error}
          </p>
        )}
        <Dialog open={busy}>
          <DialogContent
            showCloseButton={false}
            onEscapeKeyDown={(event) => event.preventDefault()}
            onInteractOutside={(event) => event.preventDefault()}
          >
            <DialogTitle>Preparing your data</DialogTitle>
            <DialogDescription>
              Complete the macOS file dialog. Keep Lyra open while it verifies and saves your data.
            </DialogDescription>
          </DialogContent>
        </Dialog>
      </div>
    </SettingsDisclosure>
  )
}
