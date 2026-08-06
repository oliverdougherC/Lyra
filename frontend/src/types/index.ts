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
  /**
   * Pages text recognition tried and could not read. A different fact from `pages_skipped`,
   * which counts pages that had no text to find, and both can be true at once.
   */
  pages_failed: number
  /** Whether this document has been asked to be read as images. */
  recognize: boolean
  error_message: string | null
  created_at: string
}

export interface DocumentStatus {
  state: DocumentState
  stage_detail: string | null
  pages_total: number | null
  pages_done: number
  pages_skipped: number
  pages_failed: number
  recognize: boolean
  error_message: string | null
}

export interface SessionRead {
  id: number
  class_id: number
  title: string | null
  mode: ChatMode
  /** The step of a solution this conversation is anchored to, pinned into every turn. */
  artifact_part_id: number | null
  created_at: string
}

export interface MessageRead {
  id: number
  session_id: number
  role: MessageRole
  content: string
  /** The model's reasoning for this turn, empty for a model that does not think. */
  thinking: string
  /** How long that reasoning took. Zero for a turn that did none. */
  thinking_ms: number
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
  /** True once the user has corrected the value, which protects it from consolidation. */
  edited: boolean
  source_document_id: number | null
  source_filename: string | null
  /** Every document that states this fact. Its length is how well evidenced the fact is. */
  sources: string[]
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
  /** Null means nobody has asked this endpoint yet, which is not the same as a no. */
  tools_supported: boolean | null
  tools_message: string | null
  /** Same three states, for whether the endpoint can read an image. */
  vision_supported: boolean | null
  vision_message: string | null
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

/**
 * The seven SSE frame shapes emitted by `POST /api/sessions/{id}/chat` and
 * `/regenerate`. `reasoning` carries a thinking model's deliberation and never overlaps
 * with `token`, which carries the answer.
 */
export type ChatEvent =
  | { type: 'start'; message_id: number }
  | { type: 'status'; stage: 'prompt_processing' | 'reviewing_documents' | 'composing_answer' }
  | { type: 'notice'; retrieval_trimmed: boolean; omitted_document_count: number }
  | { type: 'reasoning'; text: string }
  | { type: 'token'; text: string }
  | { type: 'done'; message_id: number }
  | { type: 'error'; message: string }

export interface ChatRequest {
  content: string
  mode: ChatMode
  document_id: number | null
}

/** Body of `POST /api/sessions/{id}/regenerate`: the question is the one already stored. */
export interface RegenerateRequest {
  mode: ChatMode
  document_id: number | null
}

/** Where a solution set's run has got to. `awaiting_review` is waiting on the student. */
export type SolutionState =
  'pending' | 'segmenting' | 'awaiting_review' | 'solving' | 'ready' | 'failed' | 'cancelled'

export type PartKind = 'problem' | 'step' | 'answer' | 'figure'

export type PartStatus = 'pending' | 'solving' | 'verifying' | 'complete' | 'failed'

export type PartOrigin = 'generated' | 'regenerated' | 'user_corrected'

/**
 * How a problem's lettered parts are solved. A section reading "For each system below,
 * determine whether it is linear: (a) ... (e)" is five questions with five answers, and
 * splits; a problem whose part (b) uses the result from (a) is one solution and cannot.
 */
export type SolveParts = 'together' | 'separately'

/**
 * What checking concluded. `unchecked` (checking did not run) and `uncheckable` (nothing
 * here could be checked) are both honest non-answers, and neither may render as a pass.
 */
export type Verdict = 'unchecked' | 'verified' | 'refuted' | 'uncheckable'

export type SourceRole = 'problem_set' | 'reference_solutions'

export interface SolutionSource {
  document_id: number
  role: SourceRole
  ordinal: number
  filename: string
}

/** Where a part came from. `filename` is null once the source document is deleted. */
export interface Provenance {
  chunk_id: number | null
  document_id: number | null
  page_number: number | null
  label: string | null
  filename: string | null
  /**
   * Where on the page this starts, as `[x0, y0, x1, y1]` fractions of the page box.
   * Null when the marker was never looked for or could not be found.
   */
  bbox: number[] | null
}

/** One tool call the verifier made. The audit trail behind a verdict. */
export interface SolutionCheck {
  tool: string
  arguments: string
  ok: boolean
  result: string
}

export interface SolutionRevision {
  revision: number
  content: string
  origin: PartOrigin
  note: string | null
  created_at: string
}

export interface SolutionPart {
  id: number
  artifact_id: number
  parent_part_id: number | null
  kind: PartKind
  ordinal: number
  label: string | null
  content: string
  content_type: 'markdown' | 'image'
  status: PartStatus
  origin: PartOrigin
  verdict: Verdict
  /** The sentence behind the verdict: what disagreed, or why checking did not run. */
  verdict_detail: string | null
  /**
   * On a problem with sub-parts, whether those parts are questions in their own right.
   * `separately` means each one carries its own steps, answer, and verdict and the
   * problem itself carries none; `together` means the problem holds one solution
   * answering all of them. Always `together` on anything without parts to relate.
   */
  solve_parts: SolveParts
  error_message: string | null
  provenance: Provenance[]
  checks: SolutionCheck[]
}

export interface SolutionRead {
  id: number
  class_id: number
  kind: 'solution_set'
  title: string
  state: SolutionState
  stage_detail: string | null
  /** Null until segmentation has counted, so "not counted" differs from "none found". */
  problems_total: number | null
  problems_done: number
  error_message: string | null
  created_at: string
  updated_at: string
  sources: SolutionSource[]
}

export interface SolutionDetail extends SolutionRead {
  parts: SolutionPart[]
}

export interface SolutionStatus {
  state: SolutionState
  stage_detail: string | null
  problems_total: number | null
  problems_done: number
  error_message: string | null
  parts: { id: number; status: PartStatus; verdict: Verdict }[]
}

export interface SolutionCreate {
  sources: { document_id: number; role: SourceRole }[]
  title?: string | null
}

/** One sub-part in a corrected problem list. */
export interface SegmentationPart {
  label?: string | null
  statement: string
}

/**
 * One problem in a corrected problem list. `id` names the part it came from, so an edited
 * problem keeps the page it was found on. Merge and split produce entries with no id.
 */
export interface SegmentationProblem {
  id?: number | null
  label?: string | null
  statement: string
  parts: SegmentationPart[]
}

export interface SegmentationUpdate {
  problems: SegmentationProblem[]
}

/** A text source as the solver's source pane reads it. PDFs render as page images. */
export interface DocumentText {
  filename: string
  text: string
  truncated: boolean
}

export interface ToolSupportResult {
  ok: boolean
  message: string
}
