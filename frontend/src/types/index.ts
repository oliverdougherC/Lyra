/**
 * Mirrors the backend Pydantic schemas. Kept hand-written rather than generated so a
 * backend change that is not reflected here shows up as a type error at the call site.
 */

export type DocumentState =
  | 'pending'
  | 'parsing'
  | 'chunking'
  | 'embedding'
  | 'extracting'
  | 'ready'
  | 'failed'
  | 'unsupported'

export type FactKind = 'deadline' | 'topic' | 'grading' | 'professor' | 'prerequisite' | 'note'

export type Confidence = 'high' | 'low'

export type ChatMode = 'guide' | 'show'

export type MessageRole = 'user' | 'assistant'

export type ApiKeyStorage = 'keychain' | 'file'

/** Reason the `extracting` stage did not run. Rendered as an explanation, never as an error. */
export type ExtractionSkipReason =
  'extraction_disabled' | 'no_endpoint' | 'remote_unacknowledged' | 'unparseable_response'

export interface ClassRead {
  id: number
  name: string
  code: string | null
  semester: string | null
  archived: boolean
  document_count: number
  created_at: string
  last_active_at: string
}

export interface ClassCreate {
  name: string
  code?: string | null
  semester?: string | null
}

export type ClassUpdate = Partial<ClassCreate> & { archived?: boolean }

export interface DocumentRead {
  id: number
  class_id: number
  filename: string
  mime: string
  byte_size: number
  state: DocumentState
  stage_detail: string | null
  pages_total: number | null
  pages_done: number
  pages_skipped: number
  error_message: string | null
  created_at: string
}

export interface DocumentStatus {
  state: DocumentState
  stage_detail: string | null
  pages_total: number | null
  pages_done: number
  pages_skipped: number
  error_message: string | null
}

export interface SessionRead {
  id: number
  class_id: number
  title: string | null
  mode: ChatMode
  created_at: string
}

export interface MessageRead {
  id: number
  session_id: number
  role: MessageRole
  content: string
  retrieval_trimmed: boolean
  omitted_document_count: number
  created_at: string
}

export interface FactRead {
  id: number
  class_id: number | null
  kind: FactKind
  label: string
  value: string
  confidence: Confidence
  confirmed: boolean
  rejected: boolean
  source_document_id: number | null
  source_filename: string | null
  created_at: string
}

export interface ClassProfile {
  facts: FactRead[]
  extraction_skipped_reason: ExtractionSkipReason | null
}

export interface UserProfile {
  facts: FactRead[]
}

export interface SettingsRead {
  endpoint_url: string | null
  model: string | null
  context_window: number
  extraction_enabled: boolean
  remote_ack: boolean
  api_key_set: boolean
  api_key_storage: ApiKeyStorage
  /** Null when no endpoint is configured, so "unknown" is distinct from "remote". */
  endpoint_is_local: boolean | null
  endpoint_host: string | null
  embedding_model: string | null
  embedding_dim: number | null
}

export interface SettingsUpdate {
  endpoint_url?: string | null
  model?: string | null
  context_window?: number
  extraction_enabled?: boolean
  remote_ack?: boolean
  /** Sent once and never read back. An empty string deletes the stored key. */
  api_key?: string
}

export interface ConnectionTestResult {
  ok: boolean
  model_count: number
  message: string
}

/** The six SSE frame shapes emitted by `POST /api/sessions/{id}/chat`. */
export type ChatEvent =
  | { type: 'start'; message_id: number }
  | { type: 'status'; stage: 'prompt_processing' | 'reviewing_documents' | 'composing_answer' }
  | { type: 'notice'; retrieval_trimmed: boolean; omitted_document_count: number }
  | { type: 'token'; text: string }
  | { type: 'done'; message_id: number }
  | { type: 'error'; message: string }

export interface ChatRequest {
  content: string
  mode: ChatMode
  document_id: number | null
}
