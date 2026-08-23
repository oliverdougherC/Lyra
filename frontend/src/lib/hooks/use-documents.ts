'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'
import { classKeys } from '@/lib/hooks/use-classes'
import type { DocumentRead, DocumentState } from '@/types'

export const documentKeys = {
  list: (classId: number) => ['documents', classId] as const,
  status: (documentId: number) => ['status', documentId] as const,
  outline: (documentId: number) => ['outline', documentId] as const,
}

const TERMINAL_STATES: readonly DocumentState[] = ['ready', 'failed', 'unsupported']

export function isTerminal(state: DocumentState): boolean {
  return TERMINAL_STATES.includes(state)
}

/**
 * Terminal, but not usable: waiting will not fix this document, and Lyra cannot study
 * from it. `failed` broke during ingestion; `unsupported` is a format Lyra cannot read.
 * The distinction matters on the Documents tab, where the recovery actions differ, but
 * anywhere the question is "does something here need the student?" the two are one
 * answer.
 */
const ATTENTION_STATES: readonly DocumentState[] = ['failed', 'unsupported']

export function needsAttention(state: DocumentState): boolean {
  return ATTENTION_STATES.includes(state)
}

/**
 * How a finished batch breaks down, classified by what each terminal state actually means.
 *
 * `ready` is the only terminal success; `failed` and `unsupported` both need the student,
 * so both count as needing attention - the same `needsAttention()` line the document rows
 * use, so the batch summary and the rows can never disagree about whether a document is
 * usable. Non-terminal states are ignored: this is a summary of what has settled.
 */
export type BatchOutcome = {
  /** Terminal documents Lyra can study from. */
  ready: number
  /** Terminal documents that need the student: ingestion failures and unsupported input. */
  needsAttention: number
  /** Every document that reached a terminal state. */
  settled: number
}

export function classifyBatch(states: readonly DocumentState[]): BatchOutcome {
  let ready = 0
  let attention = 0
  for (const state of states) {
    if (!isTerminal(state)) continue
    if (needsAttention(state)) attention += 1
    else ready += 1
  }
  return { ready, needsAttention: attention, settled: ready + attention }
}

/**
 * The one-line summary a finished batch shows, given how many of its items need attention.
 *
 * `attention` folds together every unusable outcome the batch produced - uploads that
 * never reached the server, ingestion failures, and unsupported input - because the
 * summary's job is only to say whether the student has to come back to something. The
 * per-file distinction between those outcomes, and the recovery each needs, lives on the
 * rows. A batch with any of them must never claim to be all-success.
 */
export function batchSummaryTitle(attention: number): string {
  if (attention === 0) return 'All documents processed'
  if (attention === 1) return '1 item needs attention'
  return `${attention} items need attention`
}

/** How often the list asks again while the server is still working on something in it. */
const IN_FLIGHT_POLL_MS = 1500

/**
 * How often to ask for the list again, given what the list currently says.
 *
 * Ingestion is a background job on the server: nothing is pushed, so the only way the
 * interface learns that a document moved from `extracting` to `ready` is to ask. Deriving
 * that from the data rather than from the caller is what makes it self-healing - the list
 * cannot be left sitting on a stage that finished minutes ago because whichever component
 * happened to mount it did not think to ask for polling.
 *
 * A caller's own interval is a floor, never a ceiling: a screen that wants the list every
 * two seconds for its own reasons still gets the faster poll while work is in flight.
 */
export function documentsPollInterval(
  documents: DocumentRead[] | undefined,
  override: number | false | undefined,
): number | false {
  const inFlight = documents?.some((document) => !isTerminal(document.state)) ?? false
  if (!inFlight) return override ?? false
  return Math.min(override || Number.POSITIVE_INFINITY, IN_FLIGHT_POLL_MS)
}

export function useDocuments(
  classId: number,
  options: { enabled?: boolean; refetchInterval?: number | false } = {},
) {
  return useQuery({
    queryKey: documentKeys.list(classId),
    queryFn: ({ signal }) => api.listDocuments(classId, signal),
    enabled: options.enabled ?? Number.isFinite(classId),
    refetchInterval: (query) => documentsPollInterval(query.state.data, options.refetchInterval),
    // Ingestion does not pause because the student switched windows, and a minute of it is
    // exactly when they would: reading a document is slow enough to go and do something
    // else. Without this the poll stops on blur and, since the app turns off
    // refetch-on-focus, coming back showed the stage it was on when they left until the
    // page was reloaded by hand.
    refetchIntervalInBackground: true,
  })
}

/**
 * Polls a single document's ingestion progress. The interval backs off from 500ms to 2s
 * so an early upload feels immediate without hammering the backend through a long
 * `extracting` stage, and stops entirely on a terminal state.
 */
export function useDocumentStatus(documentId: number, enabled = true) {
  return useQuery({
    queryKey: documentKeys.status(documentId),
    queryFn: ({ signal }) => api.getDocumentStatus(documentId, signal),
    enabled: enabled && Number.isFinite(documentId),
    refetchInterval: (query) => {
      const state = query.state.data?.state
      if (state && isTerminal(state)) return false
      const polls = query.state.dataUpdateCount
      return Math.min(500 + polls * 250, 2000)
    },
    // Same reason as the list: a backgrounded tab must not freeze a run in progress.
    refetchIntervalInBackground: true,
  })
}

export function useUploadDocument(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (file: File) => api.uploadDocument(classId, file),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: documentKeys.list(classId) })
      queryClient.invalidateQueries({ queryKey: classKeys.all })
    },
  })
}

export function useReingestDocument(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (documentId: number) => api.reingestDocument(documentId),
    onSuccess: (document) => {
      queryClient.invalidateQueries({ queryKey: documentKeys.list(classId) })
      queryClient.invalidateQueries({ queryKey: documentKeys.status(document.id) })
    },
  })
}

/**
 * Asks for a document's unreadable pages to be read as images.
 *
 * The same mutation behind `Read this document` on a scanned upload and `Try those pages`
 * on one with failures, because the backend treats them as one operation: attempt every
 * page not currently carrying text. The status query is invalidated as well as the list,
 * so the row starts polling again rather than sitting on the terminal state it was in.
 */
export function useRecognizeDocument(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (documentId: number) => api.recognizeDocument(documentId),
    onSuccess: (document) => {
      queryClient.invalidateQueries({ queryKey: documentKeys.list(classId) })
      queryClient.invalidateQueries({ queryKey: documentKeys.status(document.id) })
      queryClient.invalidateQueries({ queryKey: documentKeys.outline(document.id) })
    },
  })
}

/**
 * The structure Lyra indexed a document under. Fetched only when the disclosure is open,
 * because a closed one is the default and this is a group-by over every chunk of a book.
 */
export function useDocumentOutline(documentId: number, enabled: boolean) {
  return useQuery({
    queryKey: documentKeys.outline(documentId),
    queryFn: ({ signal }) => api.getDocumentOutline(documentId, signal),
    enabled: enabled && Number.isFinite(documentId),
  })
}

/**
 * Refiles a document under another class. Both lists are invalidated: the document leaves
 * one and arrives in the other, and the sidebar's document counts come from the class
 * list, which changes on both sides too.
 */
export function useMoveDocument(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ documentId, targetClassId }: { documentId: number; targetClassId: number }) =>
      api.moveDocument(documentId, targetClassId),
    onSuccess: (document, { targetClassId }) => {
      queryClient.invalidateQueries({ queryKey: documentKeys.list(classId) })
      queryClient.invalidateQueries({ queryKey: documentKeys.list(targetClassId) })
      queryClient.invalidateQueries({ queryKey: documentKeys.status(document.id) })
      queryClient.invalidateQueries({ queryKey: classKeys.all })
    },
  })
}

export function useDeleteDocument(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (documentId: number) => api.deleteDocument(documentId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: documentKeys.list(classId) })
      queryClient.invalidateQueries({ queryKey: classKeys.all })
    },
  })
}
