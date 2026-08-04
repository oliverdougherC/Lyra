/**
 * The only module that calls `fetch`. Components and hooks go through these functions so
 * the base URL, error shape, and abort handling exist in exactly one place.
 */

import type {
  ChatEvent,
  ChatRequest,
  ClassCreate,
  ClassProfile,
  ClassRead,
  ClassUpdate,
  ConnectionTestResult,
  DocumentRead,
  DocumentStatus,
  DocumentText,
  MessageRead,
  RegenerateRequest,
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
  ToolSupportResult,
  UserProfile,
} from '@/types'

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? 'http://127.0.0.1:8000'

/**
 * The URL of one rendered source page. An `<img src>` rather than a fetch, so the browser
 * caches it and the pane does not hold page images in memory.
 */
export function documentPageUrl(documentId: number, pageNumber: number): string {
  return `${API_BASE}/api/documents/${documentId}/pages/${pageNumber}`
}

/** A backend response that was not 2xx. `status === 0` means the request never landed. */
export class ApiError extends Error {
  readonly status: number

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
  }
}

const UNREACHABLE =
  'Could not reach the Lyra server. It runs locally, so check that scripts/dev is still running.'

type RequestOptions = {
  method?: string
  body?: unknown
  signal?: AbortSignal
}

async function send(path: string, options: RequestOptions = {}): Promise<Response> {
  const isFormData = options.body instanceof FormData
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method: options.method ?? 'GET',
      headers:
        options.body !== undefined && !isFormData
          ? { 'content-type': 'application/json' }
          : undefined,
      body: isFormData
        ? (options.body as FormData)
        : options.body !== undefined
          ? JSON.stringify(options.body)
          : undefined,
      signal: options.signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError(0, UNREACHABLE)
  }

  if (!response.ok) {
    // FastAPI errors are `{ detail: string }`; 422 bodies carry a validation array.
    let detail = `Request failed with status ${response.status}.`
    try {
      const payload = (await response.json()) as { detail?: unknown }
      if (typeof payload.detail === 'string') detail = payload.detail
      else if (Array.isArray(payload.detail) && payload.detail.length > 0) {
        const first = payload.detail[0] as { msg?: string }
        if (first.msg) detail = first.msg
      }
    } catch {
      // A non-JSON error body carries nothing useful; keep the status message.
    }
    throw new ApiError(response.status, detail)
  }

  return response
}

async function requestJson<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await send(path, options)
  return (await response.json()) as T
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

  testConnection: () =>
    requestJson<ConnectionTestResult>('/api/settings/test-connection', { method: 'POST' }),

  testTools: () => requestJson<ToolSupportResult>('/api/settings/test-tools', { method: 'POST' }),

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

  restorePartRevision: (artifactId: number, partId: number, revision: number) =>
    requestJson<SolutionPart>(`/api/solutions/${artifactId}/parts/${partId}/restore`, {
      method: 'POST',
      body: { revision },
    }),

  resegmentSolution: (artifactId: number) =>
    requestJson<SolutionRead>(`/api/solutions/${artifactId}/resegment`, { method: 'POST' }),

  cancelSolution: (artifactId: number) =>
    requestJson<SolutionRead>(`/api/solutions/${artifactId}/cancel`, { method: 'POST' }),

  deleteSolution: async (artifactId: number) => {
    await send(`/api/solutions/${artifactId}`, { method: 'DELETE' })
  },
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
): Promise<void> {
  return streamTurn(`/api/sessions/${sessionId}/chat`, body, onEvent, signal)
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

async function streamTurn(
  path: string,
  body: ChatRequest | RegenerateRequest,
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await send(path, { method: 'POST', body, signal })

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
        onEvent(JSON.parse(line.slice(5).trim()) as ChatEvent)
      } catch {
        // A frame we cannot parse is dropped rather than killing the stream.
      }
    }
  }
}
