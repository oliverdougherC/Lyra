'use client'

import { useState } from 'react'
import { Database, FolderOpen, RotateCcw } from 'lucide-react'
import { toast } from 'sonner'

import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { SettingsDisclosure } from '@/components/settings/settings-disclosure'
import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import { Spinner } from '@/components/ui/spinner'
import { ApiError } from '@/lib/api'
import {
  useCancelDesktopImport,
  useDesktopImportStatus,
  usePreviewDesktopImport,
  useResetDesktopImport,
  useStartDesktopImport,
} from '@/lib/hooks/use-settings'
import {
  pickDesktopImportDirectory,
  publishDesktopImport,
  type DesktopImportSelection,
} from '@/lib/runtime'

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  const unit = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  const value = bytes / 1024 ** unit
  return `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback
}

export function DesktopImportSection() {
  const statusQuery = useDesktopImportStatus()
  const previewImport = usePreviewDesktopImport()
  const startImport = useStartDesktopImport()
  const cancelImport = useCancelDesktopImport()
  const resetImport = useResetDesktopImport()
  const [selection, setSelection] = useState<DesktopImportSelection | null>(null)
  const [operationError, setOperationError] = useState<string | null>(null)
  const [publishing, setPublishing] = useState(false)
  const [confirmingReset, setConfirmingReset] = useState(false)

  const status = statusQuery.data
  const preview = previewImport.data ?? status?.preview ?? null
  const active = status && ['queued', 'running', 'cancel_requested'].includes(status.status)
  const staged = status?.status === 'staged' || status?.phase === 'awaiting_publish'
  const canReset =
    !active &&
    (selection !== null || preview !== null || (status != null && status.status !== 'idle'))
  const progress = status?.total_bytes
    ? Math.min(100, Math.round((status.copied_bytes / status.total_bytes) * 100))
    : 0

  async function pickFolder() {
    setOperationError(null)
    try {
      const picked = await pickDesktopImportDirectory()
      if (!picked) return
      setSelection(picked)
      await previewImport.mutateAsync(picked.selectionToken)
    } catch (error) {
      setOperationError(errorMessage(error, 'That Lyra folder could not be inspected.'))
    }
  }

  async function start() {
    setOperationError(null)
    if (!selection) return
    try {
      await startImport.mutateAsync({
        selectionToken: selection.selectionToken,
        operationId: crypto.randomUUID(),
      })
    } catch (error) {
      setOperationError(errorMessage(error, 'The import could not be started.'))
    }
  }

  async function publish() {
    setOperationError(null)
    setPublishing(true)
    try {
      const restarted = await publishDesktopImport()
      if (!restarted) throw new Error('Native publication is unavailable.')
      window.location.reload()
    } catch {
      setOperationError('The staged import was not published. Your prior data was preserved.')
      await statusQuery.refetch()
      setPublishing(false)
    }
  }

  async function reset() {
    setOperationError(null)
    try {
      const next = await resetImport.mutateAsync()
      setSelection(null)
      setConfirmingReset(false)
      setPublishing(false)
      previewImport.reset()
      toast.success(next.message ?? 'Desktop import reset.')
    } catch (error) {
      setOperationError(errorMessage(error, 'The staged import could not be discarded.'))
    }
  }

  function content() {
    if (statusQuery.isPending) {
      return (
        <div
          className="bg-muted h-24 animate-pulse rounded-md"
          aria-label="Loading import status"
        />
      )
    }

    if (statusQuery.isError) {
      return (
        <Alert variant="destructive">
          <AlertTitle>Could not load import status</AlertTitle>
          <AlertDescription>
            <p>{errorMessage(statusQuery.error, 'Check your connection and try again.')}</p>
            <Button
              variant="outline"
              size="sm"
              disabled={statusQuery.isFetching}
              onClick={() => void statusQuery.refetch()}
            >
              Retry import status
            </Button>
          </AlertDescription>
        </Alert>
      )
    }

    if (!status?.available) {
      return (
        <Alert>
          <Database />
          <AlertTitle>Available in the packaged desktop app</AlertTitle>
          <AlertDescription>
            Open Lyra&apos;s desktop app to import classes and documents from an older installation.
          </AlertDescription>
        </Alert>
      )
    }

    return (
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <Button
            id="desktop-import"
            variant="outline"
            onClick={() => void pickFolder()}
            disabled={Boolean(active) || previewImport.isPending}
          >
            {previewImport.isPending ? <Spinner /> : <FolderOpen />}
            Choose old Lyra folder
          </Button>
          {selection ? (
            <span className="text-text-secondary text-sm">{selection.label}</span>
          ) : null}
        </div>

        {!status.destination_ready ? (
          <Alert role="note">
            <AlertTitle>Import needs an empty installation</AlertTitle>
            <AlertDescription>
              Import will not overwrite classes, documents, text, or uploads already stored here.
            </AlertDescription>
          </Alert>
        ) : null}

        {preview ? (
          <div className="border-border grid gap-3 rounded-md border p-4 text-sm sm:grid-cols-2">
            <p>
              <span className="text-text-secondary block">Source</span>
              {preview.source_name}
            </p>
            <p>
              <span className="text-text-secondary block">Destination</span>
              This Lyra installation
            </p>
            <p>
              <span className="text-text-secondary block">Contents</span>
              {preview.class_count} classes · {preview.document_count} documents
            </p>
            <p>
              <span className="text-text-secondary block">Import size</span>
              {formatBytes(preview.total_bytes)} estimated
            </p>
            <details className="sm:col-span-2">
              <summary className="cursor-pointer">Technical details</summary>
              <p className="text-text-secondary mt-2">
                Database schema {preview.schema_version ?? 'detected'} · {preview.total_entries}{' '}
                files
              </p>
              {preview.asset_summary ? (
                <p className="text-text-secondary">
                  {preview.asset_summary.selected_models} model files detected;{' '}
                  {preview.asset_summary.selected_caches} cache files excluded.
                </p>
              ) : null}
            </details>
            {preview.old_runtime_active ? (
              <p className="text-danger-text sm:col-span-2">
                The old Lyra runtime appears active. Close it before importing.
              </p>
            ) : null}
            {staged ? (
              <p className="text-text-secondary sm:col-span-2">
                Ready to import. Restart to finish; your current settings, API keys, and downloaded
                models will be preserved.
              </p>
            ) : null}
            {preview.warnings.map((warning) => (
              <p key={warning} className="text-text-secondary sm:col-span-2">
                {warning}
              </p>
            ))}
          </div>
        ) : null}

        {cancelImport.isError && (
          <p role="alert" className="text-danger-text text-sm">
            Could not cancel the import. Try Cancel import again.
          </p>
        )}
        {status.message ? <p className="text-text-secondary text-sm">{status.message}</p> : null}

        {active ? (
          <div className="space-y-2" aria-live="polite">
            <Progress value={progress} aria-label={`Import ${progress}% complete`} />
            <div className="text-text-secondary flex justify-between gap-3 text-xs">
              <span>{status.phase?.replaceAll('_', ' ')}</span>
              <span>
                {formatBytes(status.copied_bytes)} / {formatBytes(status.total_bytes)}
              </span>
            </div>
            <Button
              variant="outline"
              onClick={() => cancelImport.mutate()}
              disabled={cancelImport.isPending || status.cancel_requested}
            >
              Cancel import
            </Button>
          </div>
        ) : null}

        <div className="flex flex-wrap gap-2">
          {preview && !active && !staged && status.status !== 'completed' ? (
            <Button
              onClick={() => void start()}
              disabled={
                !selection ||
                !status.destination_ready ||
                Boolean(preview.old_runtime_active) ||
                startImport.isPending
              }
            >
              {startImport.isPending ? <Spinner /> : null}
              {status.can_resume ? 'Resume import' : 'Prepare import'}
            </Button>
          ) : null}
          {staged ? (
            <Button onClick={() => void publish()} disabled={publishing}>
              {publishing ? <Spinner /> : <RotateCcw />}
              Restart and finish import
            </Button>
          ) : null}
          {canReset ? (
            <Button
              variant="outline"
              onClick={() => setConfirmingReset(true)}
              disabled={resetImport.isPending}
            >
              {resetImport.isPending ? <Spinner /> : null}
              {staged ? 'Discard staged import' : 'Reset import'}
            </Button>
          ) : null}
        </div>

        <AlertDialog open={confirmingReset} onOpenChange={setConfirmingReset}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>
                {staged ? 'Discard staged import?' : 'Reset desktop import?'}
              </AlertDialogTitle>
              <AlertDialogDescription>
                This clears Lyra&apos;s staged copy and import status only. It does not modify the
                original folder you picked, and it does not change this installation&apos;s current
                live data.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Keep it</AlertDialogCancel>
              <Button
                variant="destructive"
                disabled={resetImport.isPending}
                onClick={() => void reset()}
              >
                {resetImport.isPending ? <Spinner /> : null}
                {staged ? 'Discard staged import' : 'Reset import'}
              </Button>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
    )
  }
  return (
    <SettingsDisclosure
      title="Import existing Lyra data"
      description="Bring classes and documents from an older installation."
      anchors={['desktop-import']}
      attention={Boolean(
        active ||
        staged ||
        publishing ||
        previewImport.isPending ||
        startImport.isPending ||
        resetImport.isPending ||
        cancelImport.isPending ||
        statusQuery.isError ||
        operationError ||
        cancelImport.isError ||
        status?.status === 'failed',
      )}
    >
      {operationError && (
        <p role="alert" className="text-danger-text mb-3 text-sm">
          {operationError}
        </p>
      )}
      {content()}
    </SettingsDisclosure>
  )
}
