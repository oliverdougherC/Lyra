/**
 * The only module that calls `fetch`. Components and hooks go through these functions so
 * the base URL, error shape, and abort handling exist in exactly one place.
 */

import type {
  AcceptRejectResult,
  AgentAccessDismissalRead,
  AgentAuditEventRead,
  AgentChatActivity,
  AgentChatFailure,
  AgentChatResult,
  AgentCommandRequestRead,
  AgentConfirmationRead,
  AgentWorkspaceChangeRead,
  AgentWorkspaceGrantsUpdate,
  AgentWorkspaceRead,
  AgentProfile,
  AnswerCreate,
  AnswerRead,
  AttemptRead,
  AttemptResult,
  CurrentAttemptRead,
  CardStateRead,
  CardUpdate,
  CardUpdateRead,
  ChatEvent,
  ChatMode,
  ChatRequest,
  ClassCreate,
  ClassProfile,
  ClassRead,
  ClassUpdate,
  ClassWriterSettingsRead,
  ClassWriterSettingsUpdate,
  ConnectionTestResult,
  DeckCreate,
  DeckDetail,
  DeckSession,
  DesktopImportPreview,
  DesktopImportStatus,
  DocumentOutline,
  DocumentRead,
  DocumentStatus,
  DocumentText,
  BriefWrite,
  DraftBodyConflict,
  DraftBodySaved,
  DraftBodyUpdate,
  DraftBrief,
  DraftComment,
  DraftCommentReply,
  DraftDetail,
  DraftPlan,
  DraftPlanUpdate,
  DraftRead,
  DraftSource,
  DraftStatus,
  ExaTestResult,
  FigureRead,
  LiveDraftSuggestion,
  LiveDraftSuggestionBlock,
  MessageRead,
  PassRequest,
  PendingEdit,
  QuizCreate,
  QuizDetail,
  Rating,
  RegenerateRequest,
  ReviewRequest,
  SegmentationUpdate,
  SessionRead,
  SettingsRead,
  SettingsUpdate,
  SolutionCreate,
  SolutionDetail,
  SolutionPart,
  SolutionRead,
  SolutionRevision,
  SolutionStatus,
  StudyArtifactRead,
  StudyListRead,
  StudyStatus,
  ToolSupportResult,
  UserProfile,
  VisionSupportResult,
  WriteEvent,
  WriteRequest,
  WriterChatRequest,
} from '@/types'
import { getImmediateRuntimeConfig, getRuntimeConfig, recoverDesktopBackend } from '@/lib/runtime'

/**
 * The path of one rendered source page. Kept relative to the API base so the packaged
 * runtime can inject the loopback session contract centrally at fetch time.
 */
export function documentPagePath(documentId: number, pageNumber: number): string {
  return `/api/documents/${documentId}/pages/${pageNumber}`
}

/**
 * The path of one figure, cropped out of its page.
 *
 * Addressed by its own id rather than under its document, so a solution can draw a figure
 * knowing only what its `artifact_part` stores.
 */
export function figurePath(figureId: number): string {
  return `/api/figures/${figureId}`
}

/** A backend response that was not 2xx. `status === 0` means the request never landed. */
export class ApiError extends Error {
  readonly status: number
  readonly detail: string
  readonly code: string | undefined

  constructor(status: number, detail: string, code?: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.code = code
  }
}

export class AgentChatError extends ApiError implements AgentChatFailure {
  readonly retryable: boolean
  readonly stopped: string
  readonly activity: AgentChatActivity[]
  readonly source_ids: number[]
  readonly workspace_change_ids: number[]
  readonly command_request_ids: number[]
  readonly profile_fact_ids: number[]

  constructor(status: number, payload: AgentChatFailure) {
    super(status, payload.detail)
    this.name = 'AgentChatError'
    this.retryable = payload.retryable
    this.stopped = payload.stopped
    this.activity = payload.activity
    this.source_ids = payload.source_ids
    this.workspace_change_ids = payload.workspace_change_ids
    this.command_request_ids = payload.command_request_ids
    this.profile_fact_ids = payload.profile_fact_ids
  }
}

/**
 * A draft body write refused because the stored body moved past the version it named
 * (PLA-289). Carries the server's authoritative version and body so the workspace can keep
 * the local text and offer an honest reconciliation instead of silently reloading over it.
 */
export class DraftBodyConflictError extends ApiError {
  readonly currentVersion: number
  readonly serverBody: string

  constructor(status: number, payload: DraftBodyConflict) {
    super(status, payload.detail)
    this.name = 'DraftBodyConflictError'
    this.currentVersion = payload.current_version
    this.serverBody = payload.server_body
  }
}

function isDraftBodyConflict(payload: unknown): payload is DraftBodyConflict {
  if (!payload || typeof payload !== 'object') return false
  const value = payload as Record<string, unknown>
  return (
    value.code === 'stale_body_version' &&
    typeof value.current_version === 'number' &&
    typeof value.server_body === 'string' &&
    typeof value.detail === 'string'
  )
}

function draftBodyErrorFactory(status: number, payload: unknown | undefined): Error {
  if (status === 409 && isDraftBodyConflict(payload)) {
    return new DraftBodyConflictError(status, payload)
  }
  return defaultErrorFactory(status, payload)
}

const UNREACHABLE =
  'Could not reach the Lyra service. It runs locally when started from source, so check ./run or ./run --dev. If you opened Lyra.app, try reopening it.'

type RequestOptions = {
  method?: string
  body?: unknown
  signal?: AbortSignal
  errorFactory?: (status: number, payload: unknown | undefined) => Error
}

function readDetail(payload: unknown, status: number): string {
  const detail = `Request failed with status ${status}.`
  if (!payload || typeof payload !== 'object') return detail
  const maybe = payload as { detail?: unknown }
  if (typeof maybe.detail === 'string') return maybe.detail
  if (Array.isArray(maybe.detail) && maybe.detail.length > 0) {
    const first = maybe.detail[0] as { msg?: string }
    if (first.msg) return first.msg
  }
  return detail
}

function readCode(payload: unknown): string | undefined {
  if (!payload || typeof payload !== 'object') return undefined
  const value = payload as Record<string, unknown>
  return typeof value.code === 'string' ? value.code : undefined
}

function defaultErrorFactory(status: number, payload: unknown | undefined): ApiError {
  return new ApiError(status, readDetail(payload, status), readCode(payload))
}

function isAgentChatActivity(payload: unknown): payload is AgentChatActivity {
  if (!payload || typeof payload !== 'object') return false
  const value = payload as Record<string, unknown>
  return (
    typeof value.audit_id === 'string' &&
    typeof value.tool === 'string' &&
    typeof value.capability === 'string' &&
    typeof value.effect === 'string' &&
    typeof value.state === 'string' &&
    (typeof value.target_kind === 'string' || value.target_kind === null) &&
    (typeof value.target_id === 'string' || value.target_id === null)
  )
}

function isNumberArray(payload: unknown): payload is number[] {
  return Array.isArray(payload) && payload.every((value) => typeof value === 'number')
}

function isAgentChatFailure(payload: unknown): payload is AgentChatFailure {
  if (!payload || typeof payload !== 'object') return false
  const value = payload as Record<string, unknown>
  return (
    typeof value.detail === 'string' &&
    typeof value.retryable === 'boolean' &&
    typeof value.stopped === 'string' &&
    Array.isArray(value.activity) &&
    value.activity.every(isAgentChatActivity) &&
    isNumberArray(value.source_ids) &&
    isNumberArray(value.workspace_change_ids) &&
    isNumberArray(value.command_request_ids) &&
    isNumberArray(value.profile_fact_ids)
  )
}

function agentChatErrorFactory(status: number, payload: unknown | undefined): Error {
  return isAgentChatFailure(payload)
    ? new AgentChatError(status, payload)
    : defaultErrorFactory(status, payload)
}

function normalizeLiveDraftSuggestionStage(stage: unknown): LiveDraftSuggestion['stage'] {
  switch (stage) {
    case 'plan':
    case 'structure':
    case 'outline':
    case 'outlining':
      return 'outline'
    case 'research':
    case 'gathering':
      return 'gathering'
    case 'sections':
    case 'drafting':
      return 'drafting'
    case 'weave':
    case 'transitions':
      return 'transitions'
    case 'revise':
    case 'review':
    case 'reviewing':
      return 'review'
    case 'finalize':
    case 'finalizing':
      return 'finalize'
    case 'done':
    case 'complete':
    case 'completed':
      return 'complete'
    default:
      return typeof stage === 'string' && stage.trim()
        ? (stage as LiveDraftSuggestion['stage'])
        : 'gathering'
  }
}

function normalizeLiveDraftSuggestionStatus(status: unknown): LiveDraftSuggestion['status'] {
  switch (status) {
    case 'pending':
      return 'queued'
    case 'queued':
    case 'running':
    case 'ready':
    case 'failed':
    case 'finalized':
    case 'complete':
    case 'paused':
    case 'cancelled':
      return status
    default:
      return 'running'
  }
}

function normalizeLiveDraftSuggestionBlock(payload: unknown): LiveDraftSuggestionBlock {
  const value = payload as Record<string, unknown>
  return {
    id: Number(value.id),
    block_key: String(value.block_key ?? value.stable_key ?? ''),
    section_ref:
      value.section_ref == null || value.section_ref === '' ? null : String(value.section_ref),
    ordinal: Number(value.ordinal ?? value.paragraph_ordinal ?? 0),
    kind: String(value.kind ?? 'paragraph'),
    heading: value.heading == null || value.heading === '' ? null : String(value.heading),
    content: String(value.content ?? ''),
    status: String(value.status ?? 'queued') as LiveDraftSuggestionBlock['status'],
    target_words:
      typeof value.target_words === 'number'
        ? value.target_words
        : value.target_words == null
          ? null
          : Number(value.target_words),
    summary: value.summary == null || value.summary === '' ? null : String(value.summary),
    revision: Number(value.revision ?? 0),
    user_revision: Number(value.user_revision ?? 0),
  }
}

function normalizeLiveDraftSuggestion(payload: unknown): LiveDraftSuggestion | null {
  if (!payload || typeof payload !== 'object') return null
  const value = payload as Record<string, unknown>
  return {
    id: Number(value.id),
    artifact_id: Number(value.artifact_id),
    run_id: Number(value.run_id),
    status: normalizeLiveDraftSuggestionStatus(value.status),
    stage: normalizeLiveDraftSuggestionStage(value.stage),
    stage_detail:
      value.stage_detail == null && value.detail == null
        ? null
        : String(value.stage_detail ?? value.detail),
    version: Number(value.version ?? 0),
    base_content: String(value.base_content ?? ''),
    blocks: Array.isArray(value.blocks) ? value.blocks.map(normalizeLiveDraftSuggestionBlock) : [],
  }
}

async function send(path: string, options: RequestOptions = {}): Promise<Response> {
  const isFormData = options.body instanceof FormData
  const runtime = await getRuntimeConfig()
  const headers: Record<string, string> = {}
  if (options.body !== undefined && !isFormData) {
    headers['content-type'] = 'application/json'
  }
  if (runtime.sessionHeader) {
    headers['X-Lyra-Session'] = runtime.sessionHeader
  }
  const requestHeaders = Object.keys(headers).length > 0 ? headers : undefined
  let response: Response
  try {
    response = await fetch(`${runtime.apiBase}${path}`, {
      method: options.method ?? 'GET',
      headers: requestHeaders,
      body: isFormData
        ? (options.body as FormData)
        : options.body !== undefined
          ? JSON.stringify(options.body)
          : undefined,
      signal: options.signal,
    })
  } catch (error) {
    if (options.signal?.aborted || (error instanceof DOMException && error.name === 'AbortError')) {
      throw error
    }
    // Restart only the child the trusted Rust shell owns. The failed request is not
    // replayed automatically: a mutation may have committed before its connection broke,
    // so the existing operation-id/CAS-aware UI retry remains authoritative.
    await recoverDesktopBackend().catch(() => false)
    throw new ApiError(0, UNREACHABLE)
  }

  if (!response.ok) {
    let payload: unknown | undefined
    try {
      payload = await response.json()
    } catch {
      // A non-JSON error body carries nothing useful; keep the status message.
    }
    throw (options.errorFactory ?? defaultErrorFactory)(response.status, payload)
  }

  return response
}

async function requestJson<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await send(path, options)
  return (await response.json()) as T
}

export async function fetchProtectedAsset(path: string, signal?: AbortSignal): Promise<Blob> {
  const response = await send(path, { signal })
  return response.blob()
}

export function immediateAssetUrl(path: string): string | null {
  const runtime = getImmediateRuntimeConfig()
  if (!runtime || runtime.sessionHeader) return null
  return `${runtime.apiBase}${path}`
}

export async function loadProtectedAssetSource(
  path: string,
  signal?: AbortSignal,
): Promise<{ url: string; release?: () => void }> {
  const directUrl = immediateAssetUrl(path)
  if (directUrl) return { url: directUrl }

  const blob = await fetchProtectedAsset(path, signal)
  const url = URL.createObjectURL(blob)
  return { url, release: () => URL.revokeObjectURL(url) }
}

export const api = {
  listClasses: (signal?: AbortSignal) => requestJson<ClassRead[]>('/api/classes', { signal }),

  getClass: (classId: number, signal?: AbortSignal) =>
    requestJson<ClassRead>(`/api/classes/${classId}`, { signal }),

  createClass: (body: ClassCreate) =>
    requestJson<ClassRead>('/api/classes', { method: 'POST', body }),

  updateClass: (classId: number, body: ClassUpdate) =>
    requestJson<ClassRead>(`/api/classes/${classId}`, { method: 'PATCH', body }),

  deleteClass: async (classId: number) => {
    await send(`/api/classes/${classId}`, { method: 'DELETE' })
  },

  listDocuments: (classId: number, signal?: AbortSignal) =>
    requestJson<DocumentRead[]>(`/api/classes/${classId}/documents`, { signal }),

  uploadDocument: (classId: number, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return requestJson<DocumentRead>(`/api/classes/${classId}/documents`, {
      method: 'POST',
      body: form,
    })
  },

  getDocument: (documentId: number, signal?: AbortSignal) =>
    requestJson<DocumentRead>(`/api/documents/${documentId}`, { signal }),

  getDocumentStatus: (documentId: number, signal?: AbortSignal) =>
    requestJson<DocumentStatus>(`/api/documents/${documentId}/status`, { signal }),

  reingestDocument: (documentId: number) =>
    requestJson<DocumentRead>(`/api/documents/${documentId}/reingest`, { method: 'POST' }),

  /**
   * Reads every page of a document that has no text of its own.
   *
   * One call behind both `Read this document` and `Try those pages`: they mean the same
   * thing, because the pages that already worked are the ones this does not touch.
   */
  recognizeDocument: (documentId: number) =>
    requestJson<DocumentRead>(`/api/documents/${documentId}/recognize`, { method: 'POST' }),

  listDocumentFigures: (documentId: number, signal?: AbortSignal) =>
    requestJson<FigureRead[]>(`/api/documents/${documentId}/figures`, { signal }),

  getDocumentOutline: (documentId: number, signal?: AbortSignal) =>
    requestJson<DocumentOutline>(`/api/documents/${documentId}/outline`, { signal }),

  moveDocument: (documentId: number, classId: number) =>
    requestJson<DocumentRead>(`/api/documents/${documentId}/move`, {
      method: 'POST',
      body: { class_id: classId },
    }),

  deleteDocument: async (documentId: number) => {
    await send(`/api/documents/${documentId}`, { method: 'DELETE' })
  },

  getDocumentText: (documentId: number, signal?: AbortSignal) =>
    requestJson<DocumentText>(`/api/documents/${documentId}/text`, { signal }),

  createSession: (classId: number, artifactPartId: number | null = null) =>
    requestJson<SessionRead>(`/api/classes/${classId}/sessions`, {
      method: 'POST',
      body: { artifact_part_id: artifactPartId },
    }),

  listSessions: (classId: number, signal?: AbortSignal) =>
    requestJson<SessionRead[]>(`/api/classes/${classId}/sessions`, { signal }),

  listMessages: (sessionId: number, signal?: AbortSignal) =>
    requestJson<MessageRead[]>(`/api/sessions/${sessionId}/messages`, { signal }),

  renameSession: (sessionId: number, title: string) =>
    requestJson<SessionRead>(`/api/sessions/${sessionId}`, { method: 'PATCH', body: { title } }),

  deleteSession: async (sessionId: number) => {
    await send(`/api/sessions/${sessionId}`, { method: 'DELETE' })
  },

  getClassProfile: (classId: number, signal?: AbortSignal) =>
    requestJson<ClassProfile>(`/api/classes/${classId}/profile`, { signal }),

  correctClassFact: (classId: number, factId: number, value: string) =>
    requestJson<ClassProfile>(`/api/classes/${classId}/profile`, {
      method: 'PATCH',
      body: { fact_id: factId, value },
    }),

  resolveClassFact: (classId: number, factId: number, action: 'confirm' | 'reject') =>
    requestJson<ClassProfile>(`/api/classes/${classId}/profile/confirm`, {
      method: 'POST',
      body: { fact_id: factId, action },
    }),

  getUserProfile: (signal?: AbortSignal) => requestJson<UserProfile>('/api/profile', { signal }),

  correctUserFact: (factId: number, value: string) =>
    requestJson<UserProfile>('/api/profile', {
      method: 'PATCH',
      body: { fact_id: factId, value },
    }),

  getSettings: (signal?: AbortSignal) => requestJson<SettingsRead>('/api/settings', { signal }),

  updateSettings: (body: SettingsUpdate) =>
    requestJson<SettingsRead>('/api/settings', { method: 'PUT', body }),

  getDesktopImportStatus: (signal?: AbortSignal) =>
    requestJson<DesktopImportStatus>('/api/desktop-import/status', { signal }),

  previewDesktopImport: (selectionToken: string) =>
    requestJson<DesktopImportPreview>('/api/desktop-import/preview', {
      method: 'POST',
      body: { selection_token: selectionToken },
    }),

  startDesktopImport: (selectionToken: string, operationId: string) =>
    requestJson<DesktopImportStatus>('/api/desktop-import/start', {
      method: 'POST',
      body: { selection_token: selectionToken, operation_id: operationId },
    }),

  cancelDesktopImport: () =>
    requestJson<DesktopImportStatus>('/api/desktop-import/cancel', { method: 'POST' }),

  resetDesktopImport: () =>
    requestJson<DesktopImportStatus>('/api/desktop-import/reset', { method: 'POST' }),

  getClassWriterSettings: (classId: number, signal?: AbortSignal) =>
    requestJson<ClassWriterSettingsRead>(`/api/classes/${classId}/writer-settings`, { signal }),

  updateClassWriterSettings: (classId: number, body: ClassWriterSettingsUpdate) =>
    requestJson<ClassWriterSettingsRead>(`/api/classes/${classId}/writer-settings`, {
      method: 'PUT',
      body,
    }),

  testConnection: () =>
    requestJson<ConnectionTestResult>('/api/settings/test-connection', { method: 'POST' }),

  testTools: () => requestJson<ToolSupportResult>('/api/settings/test-tools', { method: 'POST' }),

  testVision: () =>
    requestJson<VisionSupportResult>('/api/settings/test-vision', { method: 'POST' }),

  testExa: () => requestJson<ExaTestResult>('/api/settings/test-exa', { method: 'POST' }),

  getAgentWorkspace: (classId: number, signal?: AbortSignal) =>
    requestJson<AgentWorkspaceRead | null>(`/api/classes/${classId}/workspace`, { signal }),

  attachAgentWorkspace: (
    classId: number,
    rootPath: string,
    options?: { displayName?: string; readEnabled?: boolean },
  ) =>
    requestJson<AgentWorkspaceRead>(`/api/classes/${classId}/workspace`, {
      method: 'PUT',
      body: {
        root_path: rootPath,
        display_name: options?.displayName,
        // A just-in-time attach reads the minimum it was asked for: the folder is
        // inspectable. Deeper grants (change proposals, commands) are requested separately.
        read_enabled: options?.readEnabled ?? false,
      },
    }),

  detachAgentWorkspace: async (classId: number) => {
    await send(`/api/classes/${classId}/workspace`, { method: 'DELETE' })
  },

  updateAgentWorkspaceGrants: (classId: number, body: AgentWorkspaceGrantsUpdate) =>
    requestJson<AgentWorkspaceRead>(`/api/classes/${classId}/workspace/grants`, {
      method: 'PATCH',
      body,
    }),

  listAgentActivity: (classId: number, sessionId: number, signal?: AbortSignal) =>
    requestJson<AgentAuditEventRead[]>(
      `/api/classes/${classId}/sessions/${sessionId}/agent/activity`,
      { signal },
    ),

  // A bounded "Not now" for a just-in-time access request: recorded server-side against
  // this conversation, so the card does not resurface on reload or in later turns until
  // its window lapses. It grants nothing.
  dismissAgentAccess: (classId: number, sessionId: number, scope: string) =>
    requestJson<AgentAccessDismissalRead>(
      `/api/classes/${classId}/sessions/${sessionId}/agent/access-dismiss`,
      { method: 'POST', body: { scope } },
    ),

  listAgentAccessDismissals: (classId: number, sessionId: number, signal?: AbortSignal) =>
    requestJson<{ dismissals: AgentAccessDismissalRead[] }>(
      `/api/classes/${classId}/sessions/${sessionId}/agent/access-dismissals`,
      { signal },
    ),

  // `operationId` is the browser's PLA-313 idempotency key for this logical Send: minted
  // once, carried on every ambiguous resubmit, discarded only for a genuinely new message
  // (or a structured `operation_id_mismatch`). A completed operation replays its stored
  // reply; a failed one re-runs the same durable question.
  sendAgentChat: (
    classId: number,
    sessionId: number,
    content: string,
    profile?: AgentProfile,
    documentId?: number | null,
    mode?: ChatMode,
    operationId?: string,
    signal?: AbortSignal,
  ) =>
    requestJson<AgentChatResult>(`/api/classes/${classId}/sessions/${sessionId}/agent-chat`, {
      method: 'POST',
      // The contextual turn omits the profile: the backend plans it (Workstream C). A scoped
      // source, when the student selects one, grounds the turn like the tutor's context.
      body: {
        content,
        ...(profile ? { profile } : null),
        ...(documentId != null ? { document_id: documentId } : null),
        // The student's Guide/Show choice rides the turn and is persisted on the session,
        // so the agent's shared mode contract follows the same toggle as the tutor.
        ...(mode ? { mode } : null),
        ...(operationId ? { operation_id: operationId } : null),
      },
      signal,
      errorFactory: agentChatErrorFactory,
    }),

  // Retry the conversation's last failed agent turn, reusing its user message (PLA-295).
  // The server reuses the original message rather than appending a duplicate, and replays a
  // reply that already committed instead of running the model again. The scope body is a
  // backstop only: the attempt's persisted scope (source and mode) wins.
  retryAgentChat: (
    classId: number,
    sessionId: number,
    scope?: { mode?: ChatMode; documentId?: number | null },
    signal?: AbortSignal,
  ) =>
    requestJson<AgentChatResult>(`/api/classes/${classId}/sessions/${sessionId}/agent-chat/retry`, {
      method: 'POST',
      body:
        scope == null
          ? undefined
          : {
              ...(scope.mode ? { mode: scope.mode } : null),
              // Property presence, not non-nullness (PLA-401 final pass): an explicit
              // documentId of null is the real value "All material" and must ride the wire
              // as an explicit null; only an ABSENT property means "the caller did not
              // name a scope" (the server's persisted scope then owns the turn).
              ...('documentId' in scope ? { document_id: scope.documentId } : null),
            },
      signal,
      errorFactory: agentChatErrorFactory,
    }),

  // Answer the conversation's last agent question again, replacing the reply it has (PLA-316
  // class affordance). Unlike retry, this re-runs even a completed turn and supersedes the old
  // reply on the server, so the transcript carries exactly one answer. A manual regeneration
  // carries the CURRENT Guide/Show selection and source scope (like the tutor's); a body-less
  // regeneration - the just-in-time continuation after an access approval - continues the
  // turn's persisted scope.
  regenerateAgentChat: (
    classId: number,
    sessionId: number,
    scope?: { mode?: ChatMode; documentId?: number | null },
    signal?: AbortSignal,
  ) =>
    requestJson<AgentChatResult>(
      `/api/classes/${classId}/sessions/${sessionId}/agent-chat/regenerate`,
      {
        method: 'POST',
        body:
          scope == null
            ? undefined
            : {
                ...(scope.mode ? { mode: scope.mode } : null),
                // Property presence, not non-nullness (PLA-401 final pass): an explicit
                // documentId of null is the real value "All material" and must ride the
                // wire as an explicit null - a manual regeneration to All material must
                // win over the stored document scope, and only an ABSENT property means
                // "continue the persisted scope" (the body-less JIT continuation).
                ...('documentId' in scope ? { document_id: scope.documentId } : null),
              },
        signal,
        errorFactory: agentChatErrorFactory,
      },
    ),

  // Explicit stop for the non-streaming agent turn: the handler cannot see its client's
  // disconnect, so the server cancels the in-flight task itself - settling the durable
  // attempt as stopped and releasing the session claim - and the work actually stops.
  // Stopping a session with no turn in flight is a no-op, not an error.
  //
  // The response is a quiescence claim, made truthfully - it is the verdict, not a
  // confirmation that the HTTP call succeeded:
  //   * `{ stopped: true, settling: false }` - the stop fully settled AND no worker is
  //     still inside a tool dispatch: the session is free. The UI may mark the turn
  //     stopped, clear the send key, and re-enable the conversation.
  //   * `{ stopped: false, settling: true }` - the stop was latched and the cancellation
  //     delivered, but a late worker is still inside a dispatch: the turn is stopped in
  //     every way that matters (no reply will arrive, no further durable effect can
  //     land), but the session is NOT free yet. The UI must stay in "Stopping…" and
  //     keep the conversation closed, and poll `stopAgentChatStatus` until the backend
  //     proves the session free.
  //   * `{ stopped: false, settling: false }` - nothing was in flight when /stop
  //     inspected it: the turn settled (completed or failed) in the race just before the
  //     Stop. It is NOT a stop. The turn's own request settles (or reconciles) the
  //     outcome; the durable state wins.
  stopAgentChat: (classId: number, sessionId: number, signal?: AbortSignal) =>
    requestJson<{ stopped: boolean; settling: boolean }>(
      `/api/classes/${classId}/sessions/${sessionId}/agent-chat/stop`,
      { method: 'POST', signal },
    ),

  // The bounded status read a settling Stop polls: `settling: false` is the backend's
  // proof that the stopped turn's release has finished - every worker has provably left,
  // the attempt is durably settled, and the session's turn claim is free - so this is
  // the moment the conversation re-enables.
  stopAgentChatStatus: (classId: number, sessionId: number, signal?: AbortSignal) =>
    requestJson<{ settling: boolean }>(
      `/api/classes/${classId}/sessions/${sessionId}/agent-chat/stop/status`,
      { signal },
    ),

  listAgentWorkspaceChanges: (classId: number, sessionId: number, signal?: AbortSignal) =>
    requestJson<AgentWorkspaceChangeRead[]>(
      `/api/classes/${classId}/sessions/${sessionId}/workspace/changes`,
      { signal },
    ),

  reviewAgentWorkspaceChange: (classId: number, sessionId: number, changeId: number) =>
    requestJson<AgentWorkspaceChangeRead>(
      `/api/classes/${classId}/sessions/${sessionId}/workspace/changes/${changeId}/review`,
    ),

  confirmAgentWorkspaceChange: (
    classId: number,
    sessionId: number,
    changeId: number,
    acceptedHunks: { index: number; hash: string }[],
  ) =>
    requestJson<AgentConfirmationRead>(
      `/api/classes/${classId}/sessions/${sessionId}/workspace/changes/${changeId}/confirmation`,
      { method: 'POST', body: { accepted_hunks: acceptedHunks } },
    ),

  applyAgentWorkspaceChange: (
    classId: number,
    sessionId: number,
    changeId: number,
    acceptedHunks: { index: number; hash: string }[],
    confirmationToken: string,
  ) =>
    requestJson<AgentWorkspaceChangeRead>(
      `/api/classes/${classId}/sessions/${sessionId}/workspace/changes/${changeId}/apply`,
      {
        method: 'POST',
        body: { accepted_hunks: acceptedHunks, confirmation_token: confirmationToken },
      },
    ),

  rejectAgentWorkspaceChange: (
    classId: number,
    sessionId: number,
    changeId: number,
    rejectedHunks: { index: number; hash: string }[] = [],
  ) =>
    requestJson<AgentWorkspaceChangeRead>(
      `/api/classes/${classId}/sessions/${sessionId}/workspace/changes/${changeId}/reject`,
      { method: 'POST', body: { rejected_hunks: rejectedHunks } },
    ),

  listAgentCommands: (classId: number, sessionId: number, signal?: AbortSignal) =>
    requestJson<AgentCommandRequestRead[]>(
      `/api/classes/${classId}/sessions/${sessionId}/workspace/commands`,
      { signal },
    ),

  confirmAgentCommand: (classId: number, sessionId: number, requestId: number) =>
    requestJson<AgentConfirmationRead>(
      `/api/classes/${classId}/sessions/${sessionId}/workspace/commands/${requestId}/confirmation`,
      { method: 'POST' },
    ),

  executeAgentCommand: (
    classId: number,
    sessionId: number,
    requestId: number,
    confirmationToken: string,
  ) =>
    requestJson<AgentCommandRequestRead>(
      `/api/classes/${classId}/sessions/${sessionId}/workspace/commands/${requestId}/execute`,
      { method: 'POST', body: { confirmation_token: confirmationToken } },
    ),

  rejectAgentCommand: (classId: number, sessionId: number, requestId: number) =>
    requestJson<AgentCommandRequestRead>(
      `/api/classes/${classId}/sessions/${sessionId}/workspace/commands/${requestId}/reject`,
      { method: 'POST' },
    ),

  listModels: (signal?: AbortSignal) =>
    requestJson<{ models: string[] }>('/api/settings/models', { signal }),

  listSolutions: (classId: number, signal?: AbortSignal) =>
    requestJson<SolutionRead[]>(`/api/classes/${classId}/solutions`, { signal }),

  createSolution: (classId: number, body: SolutionCreate) =>
    requestJson<SolutionRead>(`/api/classes/${classId}/solutions`, { method: 'POST', body }),

  getSolution: (artifactId: number, signal?: AbortSignal) =>
    requestJson<SolutionDetail>(`/api/solutions/${artifactId}`, { signal }),

  getSolutionStatus: (artifactId: number, signal?: AbortSignal) =>
    requestJson<SolutionStatus>(`/api/solutions/${artifactId}/status`, { signal }),

  updateSegmentation: (artifactId: number, body: SegmentationUpdate) =>
    requestJson<SolutionDetail>(`/api/solutions/${artifactId}/segmentation`, {
      method: 'PATCH',
      body,
    }),

  startSolution: (artifactId: number) =>
    requestJson<SolutionRead>(`/api/solutions/${artifactId}/start`, { method: 'POST' }),

  updateSolutionPart: (artifactId: number, partId: number, content: string) =>
    requestJson<SolutionPart>(`/api/solutions/${artifactId}/parts/${partId}`, {
      method: 'PATCH',
      body: { content },
    }),

  regenerateSolutionPart: (artifactId: number, partId: number, correction: string) =>
    requestJson<SolutionPart>(`/api/solutions/${artifactId}/parts/${partId}/regenerate`, {
      method: 'POST',
      body: { correction },
    }),

  listPartRevisions: (artifactId: number, partId: number, signal?: AbortSignal) =>
    requestJson<SolutionRevision[]>(`/api/solutions/${artifactId}/parts/${partId}/revisions`, {
      signal,
    }),

  /**
   * Restore an earlier revision. `expectedVersion` is the draft body's `content_version`
   * the caller last saw; when present the restore writes through the compare-and-swap and
   * a stale tab is refused with a `DraftBodyConflictError` rather than replacing newer text
   * (PLA-289). The solution history omits it and restores unchanged.
   */
  restorePartRevision: (
    artifactId: number,
    partId: number,
    revision: number,
    expectedVersion?: number,
  ) =>
    requestJson<SolutionPart>(`/api/solutions/${artifactId}/parts/${partId}/restore`, {
      method: 'POST',
      body:
        expectedVersion === undefined
          ? { revision }
          : { revision, expected_version: expectedVersion },
      errorFactory: draftBodyErrorFactory,
    }),

  resegmentSolution: (artifactId: number) =>
    requestJson<SolutionRead>(`/api/solutions/${artifactId}/resegment`, { method: 'POST' }),

  cancelSolution: (artifactId: number) =>
    requestJson<SolutionRead>(`/api/solutions/${artifactId}/cancel`, { method: 'POST' }),

  renameSolution: (artifactId: number, title: string) =>
    requestJson<SolutionRead>(`/api/solutions/${artifactId}`, { method: 'PATCH', body: { title } }),

  deleteSolution: async (artifactId: number) => {
    await send(`/api/solutions/${artifactId}`, { method: 'DELETE' })
  },

  listStudy: (classId: number, signal?: AbortSignal) =>
    requestJson<StudyListRead>(`/api/classes/${classId}/study`, { signal }),

  createDeck: (classId: number, body: DeckCreate) =>
    requestJson<StudyArtifactRead>(`/api/classes/${classId}/decks`, { method: 'POST', body }),

  createQuiz: (classId: number, body: QuizCreate) =>
    requestJson<StudyArtifactRead>(`/api/classes/${classId}/quizzes`, { method: 'POST', body }),

  getDeck: (deckId: number, signal?: AbortSignal) =>
    requestJson<DeckDetail>(`/api/decks/${deckId}`, { signal }),

  getQuiz: (quizId: number, signal?: AbortSignal) =>
    requestJson<QuizDetail>(`/api/quizzes/${quizId}`, { signal }),

  getDeckStatus: (deckId: number, signal?: AbortSignal) =>
    requestJson<StudyStatus>(`/api/decks/${deckId}/status`, { signal }),

  getQuizStatus: (quizId: number, signal?: AbortSignal) =>
    requestJson<StudyStatus>(`/api/quizzes/${quizId}/status`, { signal }),

  getDeckSession: (deckId: number, limit = 20, signal?: AbortSignal) =>
    requestJson<DeckSession>(`/api/decks/${deckId}/session?limit=${limit}`, { signal }),

  reviewCard: (partId: number, rating: Rating, operationId: string) =>
    requestJson<CardStateRead>(`/api/cards/${partId}/review`, {
      method: 'POST',
      body: { rating, operation_id: operationId },
    }),

  updateCard: (partId: number, body: CardUpdate) =>
    requestJson<CardUpdateRead>(`/api/cards/${partId}`, { method: 'PATCH', body }),

  deleteCard: async (partId: number) => {
    await send(`/api/cards/${partId}`, { method: 'DELETE' })
  },

  // A start is idempotent: it returns the resumable attempt when one is active, or opens a
  // fresh one, so opening a quiz twice never forks the score (PLA-277). `restart` is the
  // explicit start-over.
  startAttempt: (quizId: number, restart = false) =>
    requestJson<AttemptRead>(`/api/quizzes/${quizId}/attempts${restart ? '?restart=true' : ''}`, {
      method: 'POST',
    }),

  getCurrentAttempt: (quizId: number, signal?: AbortSignal) =>
    requestJson<CurrentAttemptRead>(`/api/quizzes/${quizId}/attempts/current`, { signal }),

  submitAnswer: (attemptId: number, body: AnswerCreate) =>
    requestJson<AnswerRead>(`/api/attempts/${attemptId}/answers`, { method: 'POST', body }),

  finishAttempt: (attemptId: number) =>
    requestJson<AttemptResult>(`/api/attempts/${attemptId}/finish`, { method: 'POST' }),

  renameDeck: (deckId: number, title: string) =>
    requestJson<StudyArtifactRead>(`/api/decks/${deckId}`, { method: 'PATCH', body: { title } }),

  renameQuiz: (quizId: number, title: string) =>
    requestJson<StudyArtifactRead>(`/api/quizzes/${quizId}`, { method: 'PATCH', body: { title } }),

  deleteDeck: async (deckId: number) => {
    await send(`/api/decks/${deckId}`, { method: 'DELETE' })
  },

  deleteQuiz: async (quizId: number) => {
    await send(`/api/quizzes/${quizId}`, { method: 'DELETE' })
  },

  listDrafts: (classId: number, signal?: AbortSignal) =>
    requestJson<DraftRead[]>(`/api/classes/${classId}/drafts`, { signal }),

  createDraft: (classId: number, title: string) =>
    requestJson<DraftRead>(`/api/classes/${classId}/drafts`, { method: 'POST', body: { title } }),

  getDraft: (draftId: number, signal?: AbortSignal) =>
    requestJson<DraftDetail>(`/api/drafts/${draftId}`, { signal }),

  renameDraft: (draftId: number, title: string) =>
    requestJson<DraftRead>(`/api/drafts/${draftId}`, { method: 'PATCH', body: { title } }),

  deleteDraft: async (draftId: number) => {
    await send(`/api/drafts/${draftId}`, { method: 'DELETE' })
  },

  /**
   * The autosave path. `snapshot: false` (the default) writes no revision, so a save every
   * second and a half does not bury the ones the student took on purpose.
   */
  updateDraftBody: (draftId: number, body: DraftBodyUpdate) =>
    requestJson<DraftBodySaved>(`/api/drafts/${draftId}/body`, {
      method: 'PATCH',
      body,
      // A stale-version 409 becomes a typed conflict the save engine reconciles.
      errorFactory: draftBodyErrorFactory,
    }),

  startDraftPass: (draftId: number, body: PassRequest = {}) =>
    requestJson<DraftRead>(`/api/drafts/${draftId}/pass`, {
      method: 'POST',
      body,
    }),

  startReview: (draftId: number, body: ReviewRequest = {}) =>
    requestJson<DraftRead>(`/api/drafts/${draftId}/review`, { method: 'POST', body }),

  cancelDraftRun: (draftId: number) =>
    requestJson<DraftStatus>(`/api/drafts/${draftId}/cancel`, { method: 'POST' }),

  getDraftPlan: (draftId: number, signal?: AbortSignal) =>
    requestJson<DraftPlan | null>(`/api/drafts/${draftId}/plan`, { signal }),

  updateDraftPlan: (draftId: number, body: DraftPlanUpdate) =>
    requestJson<DraftPlan>(`/api/drafts/${draftId}/plan`, { method: 'PUT', body }),

  listDraftSources: (classId: number, signal?: AbortSignal) =>
    requestJson<DraftSource[]>(`/api/classes/${classId}/sources`, { signal }),

  listComments: (draftId: number, signal?: AbortSignal) =>
    requestJson<DraftComment[]>(`/api/drafts/${draftId}/comments`, { signal }),

  getExportAvailability: (signal?: AbortSignal) =>
    requestJson<{ available: boolean; message: string | null }>('/api/export/availability', {
      signal,
    }),

  // A blob, not JSON: the PDF is the response. `send` raises ApiError on non-2xx, so
  // a missing binary or a failed stage arrives as its message.
  exportDraftPdf: async (draftId: number): Promise<Blob> => {
    const response = await send(`/api/drafts/${draftId}/export`, { method: 'POST' })
    return response.blob()
  },

  replyToComment: (commentId: number, body: string) =>
    requestJson<DraftCommentReply>(`/api/comments/${commentId}/replies`, {
      method: 'POST',
      body: { body },
    }),

  resolveComment: (commentId: number, resolved: boolean) =>
    requestJson<DraftComment>(`/api/comments/${commentId}/resolve`, {
      method: 'POST',
      body: { resolved },
    }),

  getDraftStatus: (draftId: number, signal?: AbortSignal) =>
    requestJson<DraftStatus>(`/api/drafts/${draftId}/status`, { signal }),

  getBrief: (draftId: number, signal?: AbortSignal) =>
    requestJson<DraftBrief | null>(`/api/drafts/${draftId}/brief`, { signal }),

  // A PUT because the student's edit replaces the brief whole; it lands confirmed,
  // because saving your own words is agreeing with them.
  putBrief: (draftId: number, body: BriefWrite) =>
    requestJson<DraftBrief>(`/api/drafts/${draftId}/brief`, { method: 'PUT', body }),

  confirmBrief: (draftId: number) =>
    requestJson<DraftBrief>(`/api/drafts/${draftId}/brief/confirm`, { method: 'POST' }),

  listDraftRevisions: (draftId: number, partId: number, signal?: AbortSignal) =>
    requestJson<SolutionRevision[]>(`/api/drafts/${draftId}/parts/${partId}/revisions`, {
      signal,
    }),

  restoreDraftRevision: (
    draftId: number,
    partId: number,
    revision: number,
    expectedVersion: number,
  ) =>
    requestJson<SolutionPart>(`/api/drafts/${draftId}/parts/${partId}/restore`, {
      method: 'POST',
      body: { revision, expected_version: expectedVersion },
      errorFactory: draftBodyErrorFactory,
    }),

  listWriterSessions: (draftId: number, signal?: AbortSignal) =>
    requestJson<SessionRead[]>(`/api/drafts/${draftId}/sessions`, { signal }),

  createWriterSession: (draftId: number) =>
    requestJson<SessionRead>(`/api/drafts/${draftId}/sessions`, { method: 'POST' }),

  getLiveDraftSuggestion: async (draftId: number, signal?: AbortSignal) =>
    normalizeLiveDraftSuggestion(
      await requestJson<LiveDraftSuggestion | null>(`/api/drafts/${draftId}/live-suggestion`, {
        signal,
      }),
    ),

  updateLiveDraftSuggestionBlock: (
    draftId: number,
    blockId: number,
    body: { content: string; expected_revision: number; base_content: string },
  ) =>
    requestJson<LiveDraftSuggestionBlock>(
      `/api/drafts/${draftId}/live-suggestion/blocks/${blockId}`,
      {
        method: 'PATCH',
        body,
      },
    ).then(normalizeLiveDraftSuggestionBlock),

  finalizeLiveDraftSuggestion: (draftId: number) =>
    requestJson<PendingEdit>(`/api/drafts/${draftId}/live-suggestion/finalize`, {
      method: 'POST',
    }),

  getPendingEdit: (draftId: number, signal?: AbortSignal) =>
    requestJson<PendingEdit | null>(`/api/drafts/${draftId}/pending`, { signal }),

  /**
   * Accept a pending edit. `expected_body_version` is the draft body version the student
   * reviewed against; a body that moved past it is refused with a `DraftBodyConflictError`
   * so an accept (or a force-replace) never silently overwrites newer text (PLA-289).
   */
  acceptPendingEdit: (
    editId: number,
    body: {
      hunk?: { index: number; hash: string }
      force?: boolean
      expected_body_version?: number
    } = {},
  ) =>
    requestJson<AcceptRejectResult>(`/api/pending-edits/${editId}/accept`, {
      method: 'POST',
      body,
      errorFactory: draftBodyErrorFactory,
    }),

  rejectPendingEdit: (editId: number, hunk?: { index: number; hash: string }) =>
    requestJson<AcceptRejectResult>(`/api/pending-edits/${editId}/reject`, {
      method: 'POST',
      body: { hunk },
    }),
}

/**
 * Streams a chat turn. `EventSource` cannot be used because it only issues GET, so the
 * SSE frames are parsed by hand off a `ReadableStream` reader, buffering partial lines
 * across chunks.
 */
export function streamChat(
  sessionId: number,
  body: ChatRequest,
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
  onResponse?: () => void,
): Promise<void> {
  return streamTurn(`/api/sessions/${sessionId}/chat`, body, onEvent, signal, onResponse)
}

/**
 * Streams a second attempt at the conversation's last question, over the same frame
 * protocol. There is no message body to send: the question is already stored, which is
 * what makes this a retry of the answer rather than a repeat of the question.
 */
export function streamRegenerate(
  sessionId: number,
  body: RegenerateRequest,
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamTurn(`/api/sessions/${sessionId}/regenerate`, body, onEvent, signal)
}

export function streamChatRetry(
  sessionId: number,
  body: RegenerateRequest,
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamTurn(`/api/sessions/${sessionId}/retry`, body, onEvent, signal)
}

/**
 * Streams one drafted passage for the editor's `/write` block. Stateless: nothing about
 * the document changes until the student accepts what streamed in, so the frames are
 * tokens and a done, and nothing else.
 */
export function streamWrite(
  draftId: number,
  body: WriteRequest,
  onEvent: (event: WriteEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamTurn(`/api/drafts/${draftId}/write`, body, onEvent, signal)
}

/**
 * Streams one writer turn over the chat frame protocol plus the writer's own frames:
 * `activity` narrating each tool call, `proposed` when a suggestion landed mid-turn,
 * `brief` when the assistant recorded its guess at the assignment.
 */
export function streamWriterChat(
  draftId: number,
  sessionId: number,
  body: WriterChatRequest,
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamTurn(`/api/drafts/${draftId}/chat/${sessionId}`, body, onEvent, signal)
}

export function streamWriterChatRetry(
  draftId: number,
  sessionId: number,
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamTurn(
    `/api/drafts/${draftId}/chat/${sessionId}/retry`,
    {} as Record<string, never>,
    onEvent,
    signal,
  )
}

async function streamTurn<StreamEvent>(
  path: string,
  body: ChatRequest | RegenerateRequest | WriteRequest | WriterChatRequest | Record<string, never>,
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
  onResponse?: () => void,
): Promise<void> {
  const response = await send(path, { method: 'POST', body, signal })
  onResponse?.()

  const reader = response.body?.getReader()
  if (!reader) throw new ApiError(0, UNREACHABLE)

  const decoder = new TextDecoder()
  let buffer = ''

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    let newline = buffer.indexOf('\n')
    while (newline !== -1) {
      const line = buffer.slice(0, newline).trim()
      buffer = buffer.slice(newline + 1)
      newline = buffer.indexOf('\n')
      if (!line.startsWith('data:')) continue
      try {
        onEvent(JSON.parse(line.slice(5).trim()) as StreamEvent)
      } catch {
        // A frame we cannot parse is dropped rather than killing the stream.
      }
    }
  }
}
