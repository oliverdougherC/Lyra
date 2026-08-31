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
    try {
      const picked = await pickDesktopImportDirectory()
      if (!picked) return
      setSelection(picked)
      await previewImport.mutateAsync(picked.selectionToken)
    } catch (error) {
      toast.error(errorMessage(error, 'That Lyra folder could not be inspected.'))
    }
  }

  async function start() {
    if (!selection) return
    try {
      await startImport.mutateAsync({
        selectionToken: selection.selectionToken,
        operationId: crypto.randomUUID(),
      })
    } catch (error) {
      toast.error(errorMessage(error, 'The import could not be started.'))
    }
  }

  async function publish() {
    setPublishing(true)
    try {
      const restarted = await publishDesktopImport()
      if (!restarted) throw new Error('Native publication is unavailable.')
      window.location.reload()
    } catch {
      toast.error('The staged import was not published. Your prior data was preserved.')
      await statusQuery.refetch()
      setPublishing(false)
    }
  }

  async function reset() {
    try {
      const next = await resetImport.mutateAsync()
      setSelection(null)
      setConfirmingReset(false)
      setPublishing(false)
      previewImport.reset()
      toast.success(next.message ?? 'Desktop import reset.')
    } catch (error) {
      toast.error(errorMessage(error, 'The staged import could not be discarded.'))
    }
  }

  if (statusQuery.isPending) {
    return (
      <div className="bg-muted h-24 animate-pulse rounded-md" aria-label="Loading import status" />
    )
  }

  if (statusQuery.isError || !status?.available) {
    return (
      <Alert>
        <Database />
        <AlertTitle>Available in the packaged desktop app</AlertTitle>
        <AlertDescription>
          Folder selection and publication stay behind Lyra&apos;s native desktop boundary.
        </AlertDescription>
      </Alert>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="outline" onClick={() => void pickFolder()} disabled={Boolean(active)}>
          {previewImport.isPending ? <Spinner /> : <FolderOpen />}
          Choose old Lyra folder
        </Button>
        {selection ? <span className="text-text-secondary text-sm">{selection.label}</span> : null}
      </div>

      {!status.destination_ready ? (
        <Alert variant="destructive">
          <AlertTitle>This installation already contains Lyra data</AlertTitle>
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
            <span className="text-text-secondary block">Database</span>
            Lyra schema {preview.schema_version ?? 'detected'} · {preview.class_count} classes ·{' '}
            {preview.document_count} documents
          </p>
          <p>
            <span className="text-text-secondary block">Durable files</span>
            {preview.total_entries} entries · {formatBytes(preview.total_bytes)} estimated
          </p>
          {preview.asset_summary ? (
            <p className="sm:col-span-2">
              <span className="text-text-secondary block">Optional assets</span>
              {preview.asset_summary.selected_models} source model files detected;{' '}
              {preview.asset_summary.selected_caches} disposable cache files excluded; current
              install models and keys are preserved.
            </p>
          ) : null}
          {preview.old_runtime_active ? (
            <p className="text-danger-text sm:col-span-2">
              The old Lyra runtime appears active. Close it before importing.
            </p>
          ) : null}
          {staged ? (
            <p className="text-text-secondary sm:col-span-2">
              Publication uses this verified staged copy only. If this install changed after
              staging, discard it and stage again before restarting.
            </p>
          ) : null}
          {preview.warnings.map((warning) => (
            <p key={warning} className="text-text-secondary sm:col-span-2">
              {warning}
            </p>
          ))}
        </div>
      ) : null}

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
            {status.can_resume ? 'Resume import' : 'Stage import'}
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
