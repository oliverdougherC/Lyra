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
  | 'extraction_disabled'
  | 'no_endpoint'
  | 'remote_unacknowledged'
  | 'unparseable_response'
  | 'endpoint_failed'
  | 'extraction_failed'

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

/**
 * What a session can be, which is one more thing than a tutor turn can request: writer
 * sessions belong to a draft's rail and never take Guide/Show turns.
 */
export type SessionMode = ChatMode | 'writer'

export interface SessionRead {
  id: number
  class_id: number
  title: string | null
  mode: SessionMode
  /** The step of a solution this conversation is anchored to, pinned into every turn. */
  artifact_part_id: number | null
  created_at: string
}

/** One tool call a writer turn made, in words: shown live and stored with the reply. */
export interface WriterActivity {
  tool: string
  label: string
  ok: boolean
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
  /** What a writer turn did on the way to this reply. Empty for tutor messages. */
  tool_activity: WriterActivity[]
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
  source_writer_id: number | null
  source_excerpt_id: number | null
  source_title: string | null
  source_url: string | null
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
  /** Whether the writer may search and snapshot public web sources. */
  allow_web_research: boolean
  /** Whether independent writer stages may issue bounded concurrent requests. */
  parallel_requests: boolean
  parallel_concurrency: number
  /** Loopback-only Firecrawl service used for public web search and snapshots. */
  firecrawl_base_url: string
  /** Enabled only after the installed Firecrawl build passes the redirect safety gate. */
  firecrawl_scrape_enabled: boolean
}

export interface SettingsUpdate {
  endpoint_url?: string | null
  model?: string | null
  context_window?: number
  extraction_enabled?: boolean
  remote_ack?: boolean
  allow_web_research?: boolean
  parallel_requests?: boolean
  parallel_concurrency?: number
  firecrawl_base_url?: string
  firecrawl_scrape_enabled?: boolean
  /** Sent once and never read back. An empty string deletes the stored key. */
  api_key?: string
}

export interface WriterCapabilities {
  allow_web_research: boolean
  parallel_requests: boolean
  parallel_concurrency: number
}

export interface WriterCapabilityOverrides {
  allow_web_research: boolean | null
  parallel_requests: boolean | null
  parallel_concurrency: number | null
}

export interface ClassWriterSettingsRead {
  overrides: WriterCapabilityOverrides
  effective: WriterCapabilities
}

export type ClassWriterSettingsUpdate = Partial<WriterCapabilityOverrides>

export interface ConnectionTestResult {
  ok: boolean
  model_count: number
  message: string
}

export interface FirecrawlTestResult {
  ok: boolean
  status: 'available' | 'temporarily_unavailable' | 'misconfigured'
  message: string
}

export interface AgentWorkspaceRead {
  id: number
  class_id: number
  root_path: string
  display_name: string
  read_enabled: boolean
  change_proposals_enabled: boolean
  commands_enabled: boolean
  created_at: string
  updated_at: string
}

export interface AgentWorkspaceGrantsUpdate {
  read_enabled?: boolean
  change_proposals_enabled?: boolean
  commands_enabled?: boolean
}

export interface AgentAuditEventRead {
  id: string
  tool: string
  capability: string
  effect: string
  state: string
  target_kind: string | null
  target_id: string | null
  error_message: string | null
  started_at: string
  finished_at: string | null
  result_summary: Record<string, unknown> | null
}

export interface AgentWorkspaceHunkRead {
  index: number
  hash: string
  lines: string[]
  decision?: 'accepted' | 'rejected'
}

export interface AgentWorkspaceChangeRead {
  id: number
  workspace_id: number
  session_id: number
  path: string
  rationale: string | null
  state: 'pending' | 'partially_applied' | 'applied' | 'rejected' | 'stale' | 'failed'
  current_hash: string
  current_content: string | null
  proposed_content: string | null
  hunks: AgentWorkspaceHunkRead[]
  created_at: string
  updated_at: string
  wrote?: boolean
}

export interface AgentCommandRequestRead {
  id: number
  workspace_id: number
  session_id: number
  argv: string[]
  relative_cwd: string
  reason: string
  expected_signal: string | null
  timeout_seconds: number
  state: 'pending' | 'running' | 'completed' | 'failed' | 'timed_out' | 'rejected' | 'abandoned'
  confirmed_at: string | null
  exit_code: number | null
  stdout_text: string | null
  stderr_text: string | null
  truncated: boolean
}

export interface AgentConfirmationRead {
  token: string
  expires_at: string
}

export type AgentProfile = 'research' | 'code' | 'command'

export interface AgentChatActivity {
  audit_id: string
  tool: string
  capability: string
  effect: string
  state: string
  target_kind: string | null
  target_id: string | null
}

export interface AgentChatResult {
  message_id: number
  content: string
  stopped: string
  detail: string
  activity: AgentChatActivity[]
  source_ids: number[]
  workspace_change_ids: number[]
  command_request_ids: number[]
  profile_fact_ids: number[]
}

export interface AgentChatFailure {
  detail: string
  retryable: boolean
  stopped: string
  activity: AgentChatActivity[]
  source_ids: number[]
  workspace_change_ids: number[]
  command_request_ids: number[]
  profile_fact_ids: number[]
}

/**
 * The SSE frame shapes emitted by `POST /api/sessions/{id}/chat`, `/regenerate`, and
 * the writer's `POST /api/drafts/{id}/chat/{session_id}`. `reasoning` carries a
 * thinking model's deliberation and never overlaps with `token`, which carries the
 * answer. The last three are the writer's alone: its turn narrates each tool call as
 * an `activity` frame, and reports a landed proposal or saved brief as its own event
 * so the rail can react without polling.
 */
export type ChatEvent =
  | { type: 'start'; message_id: number }
  | { type: 'status'; stage: 'prompt_processing' | 'reviewing_documents' | 'composing_answer' }
  | { type: 'notice'; retrieval_trimmed: boolean; omitted_document_count: number }
  | { type: 'reasoning'; text: string }
  | { type: 'token'; text: string }
  | { type: 'done'; message_id: number }
  | { type: 'error'; message: string }
  | { type: 'activity'; tool: string; label: string; ok: boolean }
  | { type: 'proposed'; edit_id: number }
  | { type: 'brief' }
  | { type: 'pass' }
  | { type: 'review' }
  | { type: 'comments' }

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

/**
 * Where any artifact's run has got to. A solution set moves through `segmenting` and
 * `solving`; decks, quizzes, and drafts share the one `generating` stage instead, because
 * their runs are a single model pass with no review gate.
 */
export type ArtifactState =
  | 'pending'
  | 'segmenting'
  | 'awaiting_review'
  | 'solving'
  | 'generating'
  | 'ready'
  | 'failed'
  | 'cancelled'

/** Where a solution set's run has got to. `awaiting_review` is waiting on the student. */
export type SolutionState = Exclude<ArtifactState, 'generating'>

/** Every kind the artifact substrate carries. Solutions are one; study tools are two more. */
export type ArtifactKind = 'solution_set' | 'flashcard_deck' | 'quiz' | 'draft'

export type PartKind =
  'problem' | 'step' | 'answer' | 'figure' | 'card' | 'quiz_question' | 'draft_body'

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

export type SourceRole = 'problem_set' | 'reference_solutions' | 'study_source'

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
   * The section this came from, titles joined. Null for a document with no structure and
   * for one indexed before sections existed, so the chip falls back to filename and page.
   */
  section_path: string | null
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
  content_type: 'markdown' | 'image' | 'json'
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

/** Whether the endpoint can read an image, which is what text recognition needs. */
export interface VisionSupportResult {
  ok: boolean
  message: string
}

/** One figure Lyra found in a document. */
export interface FigureRead {
  id: number
  document_id: number
  page_number: number
  figure_index: number
  /** `[x0, y0, x1, y1]` as fractions of the page box. */
  bbox: number[]
  label: string | null
  caption: string | null
  /**
   * What to call it: its caption's label where it has one, otherwise the page and position
   * it was found at. Most figures have no caption, and inventing a number the document does
   * not use would be worse than saying where it came from.
   */
  name: string
}

/** One addressable section of a document, as its indexed chunks record it. */
export interface OutlineSection {
  path: string
  number: string | null
  /** How many titles the path is made of. 1 is a chapter. */
  depth: number
  first_page: number | null
  last_page: number | null
  chunk_count: number
}

/**
 * What structure Lyra found in a document. `sectioned_count` against `chunk_count` is the
 * honest part: a book read as one flat blob has sections for none of its chunks.
 */
export interface DocumentOutline {
  sections: OutlineSection[]
  chunk_count: number
  sectioned_count: number
}

/** The four ratings a flashcard review accepts. */
export type Rating = 'again' | 'hard' | 'good' | 'easy'

/** A card's place in the scheduler: `review` is the long-term state. */
export type CardSchedulingState = 'new' | 'learning' | 'relearning' | 'review'

/** The deck panel's grouping of a card: fresh, still being learned, or mastered. */
export type CardBucket = 'new' | 'learning' | 'mastered'

/** One card's scheduling state as the API reads it, storage timestamps and all. */
export interface CardStateRead {
  due_at: string
  stability: number
  difficulty: number
  reps: number
  lapses: number
  state: CardSchedulingState
  last_review_at: string | null
  bucket: CardBucket
}

/** The artifact fields every deck and quiz carry. A study run never segments or solves. */
export interface StudyArtifactRead {
  id: number
  class_id: number
  kind: 'flashcard_deck' | 'quiz'
  title: string
  state: ArtifactState
  stage_detail: string | null
  /** Null until generation has counted, so "not counted" differs from "none found". */
  problems_total: number | null
  problems_done: number
  error_message: string | null
  created_at: string
  updated_at: string
}

/** A deck in the study list, with the scheduling counts the panel shows. */
export interface DeckSummary extends StudyArtifactRead {
  kind: 'flashcard_deck'
  cards_total: number
  due_count: number
  buckets: Record<CardBucket, number>
}

/** Everything the class study panel lists. */
export interface StudyListRead {
  decks: DeckSummary[]
  quizzes: StudyArtifactRead[]
}

/** What one flashcard says. */
export interface CardContent {
  front: string
  back: string
  topic: string
}

/** One card in a deck. `card_state` is null until the scheduler has a row for it. */
export interface DeckCard {
  part_id: number
  ordinal: number
  label: string | null
  card: CardContent
  card_state: CardStateRead | null
}

export interface DeckDetail extends StudyArtifactRead {
  kind: 'flashcard_deck'
  cards: DeckCard[]
}

/** One card in a study session, in the order the scheduler chose. */
export interface SessionCard {
  part_id: number
  label: string | null
  card: CardContent
  due: boolean
  card_state: CardStateRead
}

export interface DeckSession {
  cards: SessionCard[]
}

export type QuizDifficulty = 'basic' | 'intermediate' | 'exam'

export type QuizQuestionType = 'mcq' | 'true_false' | 'fill_blank'

/**
 * What one quiz question asks, answer included. Lyra is local and trusts the user; the
 * interface, not the API, controls when the answer is revealed.
 */
export interface QuizQuestion {
  type: QuizQuestionType
  question: string
  options: string[]
  correct_index: number
  explanation: string
  topic: string
  difficulty: QuizDifficulty
}

export interface QuizQuestionRead {
  part_id: number
  ordinal: number
  label: string | null
  question: QuizQuestion
}

export interface QuizDetail extends StudyArtifactRead {
  kind: 'quiz'
  questions: QuizQuestionRead[]
}

/** The polled generation state of one deck or quiz. Skinny on purpose: the panel polls it. */
export interface StudyStatus {
  state: ArtifactState
  stage_detail: string | null
  problems_total: number | null
  problems_done: number
  error_message: string | null
}

export interface DeckCreate {
  title: string
  /** Null means every ready document in the class. */
  document_ids?: number[] | null
  cards_per_topic?: number
}

export interface QuizCreate {
  title: string
  /** Null means every ready document in the class. */
  document_ids?: number[] | null
  count?: number
  difficulty?: QuizDifficulty
  /** Null means every question type. */
  types?: QuizQuestionType[] | null
}

/** Body of `PATCH /api/cards/{partId}`: both faces and the topic, every time. */
export interface CardUpdate {
  front: string
  back: string
  topic: string
}

export interface CardUpdateRead {
  part_id: number
  card: CardContent
}

/**
 * Body of `POST /api/attempts/{attemptId}/answers`. For a fill_blank question the runner
 * compares the typed answer against `options[0]` itself and sends 0 on a match, -1 on a miss.
 */
export interface AnswerCreate {
  part_id: number
  selected_index: number
}

export interface AttemptRead {
  attempt_id: number
  question_part_ids: number[]
}

export interface AnswerRead {
  correct: boolean
  correct_index: number
  explanation: string
}

/** A finished attempt's score, per topic: the weakness surface. */
export interface AttemptResult {
  score: number
  total: number
  by_topic: { topic: string; correct: number; total: number }[]
}

/**
 * The artifact fields a draft carries. A draft is born `ready` - there is no ingestion
 * run - and goes back to `generating` only while a suggestion pass is working.
 */
export interface DraftRead {
  id: number
  class_id: number
  kind: 'draft'
  title: string
  state: ArtifactState
  stage_detail: string | null
  problems_total: number | null
  problems_done: number
  error_message: string | null
  created_at: string
  updated_at: string
}

/** A draft with its one body part unfolded into it, which is how the workspace reads it. */
export interface DraftDetail extends DraftRead {
  part_id: number
  body: string
  /** Whether a suggestion is waiting to be reviewed, so the rail can open on it. */
  pending: boolean
}

/** The polled state of one draft's suggestion run. Skinny on purpose: the workspace polls it. */
export interface DraftStatus {
  state: ArtifactState
  stage_detail: string | null
  error_message: string | null
  /** Steps the run has: sections for a draft pass, lenses for a review. Null until counted. */
  problems_total: number | null
  problems_done: number
  /** Explicit run metadata; older servers may omit it or report `legacy`. */
  run_id?: number | null
  job_kind?: 'pass' | 'review' | null
  depth?: WriterDepth | null
  started_at?: string | null
  run_status?:
    | 'queued'
    | 'running'
    | 'cancel_requested'
    | 'completed'
    | 'failed'
    | 'cancelled'
    | 'legacy'
    | null
  cancel_requested?: boolean
  cancel_requested_at?: string | null
  finished_at?: string | null
  warnings?: DraftStatusWarning[]
}

export interface DraftStatusWarning {
  code: string
  message: string
}

/**
 * One review unit of a base/proposed diff. The coordinates are 0-based line offsets the
 * server computes; the interface renders `lines` and echoes `{index, hash}` back, and the
 * hash is what catches the hunk set moving under it.
 */
export interface Hunk {
  index: number
  old_start: number
  old_lines: number
  new_start: number
  new_lines: number
  /** Unified-diff style: ' ' context, '-' removed, '+' added, line endings stripped. */
  lines: string[]
  hash: string
}

/** A suggestion waiting for review, as `GET /api/drafts/{id}/pending` reads it. */
export interface PendingEdit {
  id: number
  stale: boolean
  /** The instruction the student gave, which is the panel's title. */
  note: string | null
  hunks: Hunk[]
  proposed_content: string
  /** Carried only when the edit is stale, for the side-by-side view. */
  base_content?: string
}

/** Body of `POST /api/drafts/{id}/write`: the instruction plus what the editor gathered. */
export interface WriteRequest {
  instruction: string
  heading?: string | null
  selection?: string | null
  nearby?: string | null
}

/** The three SSE frames `POST /api/drafts/{id}/write` emits. */
export type WriteEvent =
  { type: 'token'; text: string } | { type: 'done' } | { type: 'error'; message: string }

/** Body of `PATCH /api/drafts/{id}/body`. Without `snapshot` no revision is written. */
export interface DraftBodyUpdate {
  content: string
  snapshot?: boolean
  note?: string
}

/**
 * What the document is, kept where every writer prompt can reach it. Proposed by the
 * assistant, confirmed by the student; a proposed brief is usable but flagged as a
 * guess until confirmed.
 */
export interface DraftBrief {
  artifact_id: number
  assignment_type: string
  summary: string
  audience: string
  length_target: string
  /** The handout the brief was discerned from, when it was. Survives its deletion as null. */
  source_document_id: number | null
  status: 'proposed' | 'confirmed'
  created_at: string
  updated_at: string
}

/** Body of `PUT /api/drafts/{id}/brief` - the student's own edit, which lands confirmed. */
export interface BriefWrite {
  assignment_type?: string
  summary?: string
  audience?: string
  length_target?: string
  source_document_id?: number | null
}

/** Body of `POST /api/drafts/{id}/chat/{session_id}`: the writer turn carries no mode. */
export interface WriterChatRequest {
  content: string
}

/**
 * Body of `POST /api/drafts/{id}/pass`. Everything optional on purpose: empty is the
 * full draft pass, an instruction is a lens over it, `sections` filters it.
 */
export type WriterDepth = 'quick' | 'standard' | 'deep'

export interface PassRequest {
  instruction?: string | null
  sections?: string[]
  depth?: WriterDepth
  pause_at_plan?: boolean
  /** Resolve this finding only after the targeted pass lands successfully. */
  address_comment_id?: number
}

export interface ReviewRequest {
  depth?: WriterDepth
}

export interface DraftPlanSection {
  id: number
  section_ref: string
  ordinal: number
  title: string
  job: string
  claim: string
  evidence: string[]
  sources: number[]
  word_budget: number | null
  research_notes: string
}

/** Argument-map entries stay open-ended so the planner can add typed relations over time. */
export type DraftArgumentMapEntry = Record<string, unknown>

export interface DraftPlan {
  id: number
  artifact_id: number
  version: number
  status: string
  brief_analysis: string
  thesis: string
  argument_map: DraftArgumentMapEntry[]
  sections: DraftPlanSection[]
  created_at: string
  updated_at: string
}

export interface DraftPlanUpdate {
  brief_analysis: string
  thesis: string
  argument_map: DraftArgumentMapEntry[]
  sections: DraftPlanSection[]
}

export interface SourceExcerpt {
  id: number
  section_ref: string | null
  excerpt: string
}

export interface DraftSource {
  id: number
  class_id: number
  source_type: 'course' | 'web'
  document_id: number | null
  url: string | null
  title: string
  accessed_at: string | null
  excerpts: SourceExcerpt[]
}

export type CommentSeverity = 'critical' | 'major' | 'minor' | 'note'

export type CommentAuthor = 'reviewer' | 'writer' | 'student'

/** Where a thread's quote sits in the body as the server last resolved it. */
export interface CommentAnchor {
  start: number
  end: number
  /** False when the match came through the whitespace-normalized fallback. */
  exact: boolean
}

/** One reply under a thread root. Replies carry no severity and no anchor. */
export interface DraftCommentReply {
  id: number
  author: CommentAuthor
  body: string
  created_at: string
}

/**
 * One margin-comment thread, anchored by verbatim quote rather than position: the
 * server re-resolves the quote on every read, and a passage that is gone leaves the
 * thread orphaned rather than lost.
 */
export interface DraftComment {
  id: number
  author: CommentAuthor
  severity: CommentSeverity | null
  /** The quoted passage, or null for a whole-document finding. */
  quote: string | null
  body: string
  resolved: 0 | 1
  orphaned: 0 | 1
  anchor: CommentAnchor | null
  /** Stable plan section used by Address to target the right pass. */
  section_ref?: string | null
  replies: DraftCommentReply[]
  created_at: string
}

/** What accepting or rejecting a pending edit answers: how many hunks are left of it. */
export interface AcceptRejectResult {
  remaining: number
  /** The edit as it now stands, present whenever anything is left of it. */
  edit?: PendingEdit
}
