import { type Page, type Route } from '@playwright/test'

/**
 * Synthetic backend rows for the KaTeX cascade suite (PLA-504).
 *
 * One class, one conversation, one problem set and one draft, all with the mathematical
 * shapes the cascade has to hold: inline maths inside prose, display fractions and radicals,
 * a display equation inside a one-line preview, and a long equation that has to overflow its
 * own box. Nothing here is a real student's material.
 */

export const CLASS_ID = 12
export const SESSION_ID = 4
export const SOLUTION_ID = 21
export const DRAFT_ID = 31
export const DRAFT_SESSION_ID = 5
export const TWIN_SESSION_ID = 6
export const TWIN_MESSAGE_ID = 31
export const TWIN_NEW_QUESTION_ID = 60
export const TWIN_NEW_ANSWER_ID = 61

/** Synthetic teaching material: a signals-and-systems answer with the shapes that matter. */
export const CHAT_ANSWER = [
  'The impulse response is $h(t)=e^{-2t}u(t-3)$, so its transform is $H(s)=\\frac{1}{s+2}$',
  'with one pole at $s=-2$.',
  '',
  'Partial fractions give',
  '',
  '$$G(s)=\\frac{s+1}{(s+2)(s+3)}=\\frac{1}{s+2}-\\frac{2}{s+3}.$$',
  '',
  'and the damping ratio is $\\zeta=\\frac{1}{\\sqrt{2}}$, which is $\\overline{0.707}$.',
  '',
  '$$\\zeta=\\frac{\\ln 2}{\\sqrt{\\pi^{2}+\\ln^{2} 2}}$$',
  '',
  '$$y(t)=\\int_{-\\infty}^{\\infty} h(\\tau)\\,x(t-\\tau)\\,d\\tau=\\int_{0}^{t} e^{-2\\tau}\\cos(3(t-\\tau))\\,d\\tau+\\frac{s+5}{s^{2}+6s+13}+\\frac{s+7}{s^{2}+8s+17}+\\frac{s+9}{s^{2}+10s+21}$$',
].join('\n')

export const CHAT_QUESTION = 'Where does the pole of $H(s)=\\frac{1}{s+2}$ sit, and is it stable?'

/**
 * The renderer twin (PLA-499/PLA-500): one answer carrying every container a lifted display
 * equation can sit in — a list item, a nested item, a blockquote list, an ordered list — plus
 * the visuals the reveal cascade treats as single units: code, a table, a rule, and checkboxes.
 *
 * It serves two roles. Settled, it is the static render the streaming twin must end on.
 * Streamed, it is the live answer the cascade schedules: the same string, word by word.
 */
export const STREAM_TWIN = [
  'Here is the full shape set.',
  '',
  '- First item with $x(t)=t e^{-t}$ inline',
  '- Second item with a display fraction',
  '  $$\\frac{1}{s+2}$$',
  '  still inside the item',
  '- Third',
  '  - inner with $y=2$',
  '> - Quoted item',
  '>   $$\\zeta=\\frac{1}{2}$$',
  '1. Ordered first with $\\alpha_1$',
  '2. Ordered second',
  '',
  '```js',
  'const rate = 1 / (s + 2)',
  '```',
  '',
  '| Signal | Decay |',
  '| - | - |',
  '| $e^{-2t}$ | $2$ |',
  '',
  '- [ ] verify the residue',
  '- [x] check the poles',
  '',
  '---',
  '',
  'Done.',
].join('\n')

export const TWIN_QUESTION = 'Show the full shape set: lists, quotes, code, a table, and the rest.'

/** The streamed replay lands below the seeded twin as a new message with new IDs. */
export function twinNewRows(question: string): Record<string, unknown>[] {
  return [
    {
      id: TWIN_NEW_QUESTION_ID,
      session_id: TWIN_SESSION_ID,
      role: 'user',
      content: question,
      thinking: '',
      thinking_ms: 0,
      retrieval_trimmed: false,
      omitted_document_count: 0,
      tool_activity: [],
      created_at: '2026-08-22T09:01:00Z',
    },
    {
      id: TWIN_NEW_ANSWER_ID,
      session_id: TWIN_SESSION_ID,
      role: 'assistant',
      content: STREAM_TWIN,
      thinking: '',
      thinking_ms: 0,
      retrieval_trimmed: false,
      omitted_document_count: 0,
      tool_activity: [],
      created_at: '2026-08-22T09:01:30Z',
    },
  ]
}

/**
 * The real-incremental streaming fixture (review round 2): the content the real SSE server
 * feeds the page in delayed chunks. It carries the cases the review asks the live renderer
 * to prove: a pair of prices that must stay prose, a closed emphasis that must not re-hide
 * its words when it closes, a display block inside a list item, digit-led mathematics that
 * stays mathematics, an atomic code block, and an unfinished equation at the tail.
 */
export const VISUAL_SESSION_ID = 7
export const VISUAL_HISTORY_QUESTION_ID = 40
export const VISUAL_HISTORY_ANSWER_ID = 41
export const VISUAL_NEW_QUESTION_ID = 50
export const VISUAL_NEW_ANSWER_ID = 51
export const VISUAL_STREAM = [
  'The cost line: It costs $5 and $10 today.',
  '',
  '- **bold words** in a list item',
  '- a list item with a display fraction',
  '  $$\\frac{1}{s+2}$$',
  '',
  'It costs $5x$ per unit and $2+2$ total.',
  '',
  '```js',
  'const rate = 1 / (s + 2)',
  '```',
  '',
  'The limit is $\\frac{1}{',
].join('\n')

export const VISUAL_SESSION = {
  id: VISUAL_SESSION_ID,
  class_id: CLASS_ID,
  title: 'Real incremental streaming',
  mode: 'guide',
  artifact_part_id: null,
  created_at: '2026-09-08T09:00:00Z',
}

export const VISUAL_HISTORY_QUESTION = 'Walk through the costs and the limit.'
export const VISUAL_HISTORY_ANSWER =
  'The earlier answer: it costs $5 today, and the limit stays finite.'

/**
 * The session's history before the live turn: an earlier question and an earlier, shorter
 * answer. The streamed turn must land below it as new rows with new IDs — the persistence
 * proof is that the list gains rows, not that it already held the streamed answer under a
 * name the seed chose.
 */
export const VISUAL_HISTORY = [
  {
    id: VISUAL_HISTORY_QUESTION_ID,
    session_id: VISUAL_SESSION_ID,
    role: 'user',
    content: VISUAL_HISTORY_QUESTION,
    thinking: '',
    thinking_ms: 0,
    retrieval_trimmed: false,
    omitted_document_count: 0,
    tool_activity: [],
    created_at: '2026-09-08T09:00:00Z',
  },
  {
    id: VISUAL_HISTORY_ANSWER_ID,
    session_id: VISUAL_SESSION_ID,
    role: 'assistant',
    content: VISUAL_HISTORY_ANSWER,
    thinking: '',
    thinking_ms: 0,
    retrieval_trimmed: false,
    omitted_document_count: 0,
    tool_activity: [],
    created_at: '2026-09-08T09:00:30Z',
  },
]

/** The rows the live turn saves: new IDs, the question the composer sent, the streamed answer. */
export const VISUAL_NEW_QUESTION = 'Stream the visual shapes.'
export function visualNewRows(): Record<string, unknown>[] {
  return [
    {
      id: VISUAL_NEW_QUESTION_ID,
      session_id: VISUAL_SESSION_ID,
      role: 'user',
      content: VISUAL_NEW_QUESTION,
      thinking: '',
      thinking_ms: 0,
      retrieval_trimmed: false,
      omitted_document_count: 0,
      tool_activity: [],
      created_at: '2026-09-08T09:01:00Z',
    },
    {
      id: VISUAL_NEW_ANSWER_ID,
      session_id: VISUAL_SESSION_ID,
      role: 'assistant',
      content: VISUAL_STREAM,
      thinking: '',
      thinking_ms: 0,
      retrieval_trimmed: false,
      omitted_document_count: 0,
      tool_activity: [],
      created_at: '2026-09-08T09:01:30Z',
    },
  ]
}

export const TWIN_MESSAGES = [
  {
    id: 30,
    session_id: TWIN_SESSION_ID,
    role: 'user',
    content: TWIN_QUESTION,
    thinking: '',
    thinking_ms: 0,
    retrieval_trimmed: false,
    omitted_document_count: 0,
    tool_activity: [],
    created_at: '2026-08-22T09:00:00Z',
  },
  {
    id: TWIN_MESSAGE_ID,
    session_id: TWIN_SESSION_ID,
    role: 'assistant',
    content: STREAM_TWIN,
    thinking: '',
    thinking_ms: 0,
    retrieval_trimmed: false,
    omitted_document_count: 0,
    tool_activity: [],
    created_at: '2026-08-22T09:00:30Z',
  },
]

export const TWIN_SESSION = {
  id: TWIN_SESSION_ID,
  class_id: CLASS_ID,
  title: 'Renderer shapes',
  mode: 'guide',
  artifact_part_id: null,
  created_at: '2026-08-22T08:59:00Z',
}

export const CLASS_12 = {
  id: CLASS_ID,
  name: 'Continuous-Time Signals',
  code: 'ECE 203',
  semester: 'Fall 2026',
  archived: false,
  document_count: 1,
  created_at: '2026-08-01T09:00:00Z',
  last_active_at: '2026-08-30T18:15:00Z',
}

export const MESSAGES = [
  {
    id: 1,
    session_id: SESSION_ID,
    role: 'user',
    content: CHAT_QUESTION,
    thinking: '',
    thinking_ms: 0,
    retrieval_trimmed: false,
    omitted_document_count: 0,
    tool_activity: [],
    created_at: '2026-08-20T10:01:00Z',
  },
  {
    id: 2,
    session_id: SESSION_ID,
    role: 'assistant',
    content: CHAT_ANSWER,
    thinking: '',
    thinking_ms: 0,
    retrieval_trimmed: false,
    omitted_document_count: 0,
    tool_activity: [],
    created_at: '2026-08-20T10:01:20Z',
  },
]

// The pane disables the composer without `endpoint_url`, so the settings carry the real
// SettingsRead shape (backend/api/routes_settings.py): a configured local tutor endpoint.
export const SETTINGS = {
  endpoint_url: 'http://127.0.0.1:9000/v1',
  model: 'synthetic',
  context_window: 8192,
  extraction_enabled: false,
  remote_ack: true,
  api_key_set: true,
  api_key_storage: 'file',
  endpoint_is_local: true,
  endpoint_host: '127.0.0.1',
  embedding_model: null,
  embedding_dim: null,
  tools_supported: null,
  tools_message: null,
  vision_supported: null,
  vision_message: null,
  allow_web_research: false,
  parallel_requests: true,
  parallel_concurrency: 2,
  exa_api_key_set: false,
  exa_api_key_storage: 'file',
  local_model_setup: '',
}

export const STATUS = {
  state: 'ready',
  stage_detail: null,
  error_message: null,
  problems_total: null,
  problems_done: 0,
  run_id: null,
  job_kind: null,
  depth: null,
  started_at: null,
  run_status: null,
  cancel_requested: false,
  cancel_requested_at: null,
  finished_at: null,
  warnings: [],
}

export const DRAFT_DETAIL = {
  id: DRAFT_ID,
  class_id: CLASS_ID,
  kind: 'draft',
  title: 'Laplace transforms, first draft',
  state: 'ready',
  stage_detail: null,
  problems_total: null,
  problems_done: 0,
  error_message: null,
  created_at: '2026-08-21T00:00:00Z',
  updated_at: '2026-08-21T00:00:00Z',
  part_id: 40,
  // The draft body carries its own display maths so the editor chunk is asked to typeset
  // the same constructs the chat is.
  body: 'We start from $x(t)=e^{-2t}u(t)$.\n\n$$X(s)=\\frac{1}{s+2}.$$\n',
  body_version: 3,
  pending: false,
}

/**
 * A comment whose quoted passage is a display equation: the inline preview in the margin
 * has to hold it on one line.
 */
export const DRAFT_COMMENTS = [
  {
    id: 71,
    artifact_id: DRAFT_ID,
    part_id: 40,
    author: 'reviewer' as const,
    body: 'This is the transform you quoted; check the region of convergence.',
    quote: '$$X(s)=\\frac{1}{s+2}$$',
    anchor_start: 0,
    anchor_end: 0,
    severity: 'minor' as const,
    resolved: false,
    created_at: '2026-08-21T00:00:00Z',
    replies: [],
  },
]

export const SOLUTION_DETAIL = {
  id: SOLUTION_ID,
  class_id: CLASS_ID,
  kind: 'solution_set',
  title: 'Homework 2',
  state: 'awaiting_review',
  stage_detail: null,
  problems_total: 2,
  problems_done: 0,
  error_message: null,
  created_at: '2026-08-05T08:00:00Z',
  updated_at: '2026-08-05T09:30:00Z',
  sources: [{ document_id: 101, role: 'problem_set', ordinal: 0, filename: 'Homework 2.pdf' }],
  parts: [
    {
      id: 10,
      artifact_id: SOLUTION_ID,
      parent_part_id: null,
      ordinal: 1,
      label: 'Problem 1',
      content: 'Find the Fourier transform of each signal.',
      content_type: 'markdown',
      kind: 'problem',
      status: 'pending',
      origin: 'generated',
      verdict: 'unchecked',
      verdict_detail: null,
      solve_parts: 'together',
      error_message: null,
      checks: [],
      provenance: [],
    },
    {
      id: 11,
      artifact_id: SOLUTION_ID,
      parent_part_id: 10,
      ordinal: 2,
      label: '(a)',
      // Block maths inside a review row: the row is a list row, and the equation is a
      // display equation, which is exactly the pairing that used to print raw TeX.
      content: '$$\nx(t)=e^{-2t}u(t-3)\n$$',
      content_type: 'markdown',
      kind: 'problem',
      status: 'pending',
      origin: 'generated',
      verdict: 'unchecked',
      verdict_detail: null,
      solve_parts: 'together',
      error_message: null,
      checks: [],
      provenance: [],
    },
    {
      id: 12,
      artifact_id: SOLUTION_ID,
      parent_part_id: 10,
      ordinal: 3,
      label: '(b)',
      content: '$x(t)=t e^{-2t}u(t)$',
      content_type: 'markdown',
      kind: 'problem',
      status: 'pending',
      origin: 'generated',
      verdict: 'unchecked',
      verdict_detail: null,
      solve_parts: 'together',
      error_message: null,
      checks: [],
      provenance: [],
    },
  ],
}

export type Responder = (route: Route) => Promise<void> | void

/**
 * A session whose message list grows while the test runs: the turn's result is persisted
 * as new rows with new IDs, and the pane's post-turn refetch has to see them. `initial` is
 * what the session holds before the turn; `append` is what the feed server calls when it
 * saves the turn. `served` is the exact list the pane last fetched — the persistence proof
 * is read off the wire, not off a seed.
 */
export type LiveMessages = {
  sessionId: number
  append: (rows: unknown[]) => void
  served: () => unknown[]
  rows: () => unknown[]
}

/**
 * The backend, at the network boundary, with synthetic rows only. Anything not named here
 * answers as an empty list so a stray read cannot render an error state the cascade test is
 * not measuring; `apiCalls` records what the running app actually asked for, so fixture
 * drift stays visible.
 */
export async function installLyraApi(
  page: Page,
  extra: Record<string, unknown> = {},
  live?: { sessionId: number; initial: unknown[] },
) {
  const json = (route: Route, body: unknown) =>
    route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
  const apiCalls: string[] = []
  let liveRows: unknown[] | null = live ? [...live.initial] : null
  let liveServed: unknown[] | null = null
  const handlers: Record<string, unknown> = {
    '/api/classes': [CLASS_12],
    [`/api/classes/${CLASS_ID}`]: CLASS_12,
    [`/api/classes/${CLASS_ID}/documents`]: [],
    [`/api/classes/${CLASS_ID}/sessions`]: [
      {
        id: SESSION_ID,
        class_id: CLASS_ID,
        title: 'Poles and stability',
        mode: 'guide',
        artifact_part_id: null,
        created_at: '2026-08-20T10:00:00Z',
      },
      TWIN_SESSION,
    ],
    [`/api/classes/${CLASS_ID}/solutions`]: [SOLUTION_DETAIL],
    [`/api/classes/${CLASS_ID}/study`]: { decks: [], quizzes: [] },
    [`/api/classes/${CLASS_ID}/drafts`]: [
      {
        id: DRAFT_ID,
        class_id: CLASS_ID,
        kind: 'draft',
        title: DRAFT_DETAIL.title,
        state: 'ready',
        stage_detail: null,
        problems_total: null,
        problems_done: 0,
        error_message: null,
        created_at: DRAFT_DETAIL.created_at,
        updated_at: DRAFT_DETAIL.updated_at,
      },
    ],
    [`/api/classes/${CLASS_ID}/profile`]: { facts: [], extraction_skipped_reason: null },
    [`/api/classes/${CLASS_ID}/workspace`]: null,
    [`/api/sessions/${SESSION_ID}`]: {
      id: SESSION_ID,
      class_id: CLASS_ID,
      title: 'Poles and stability',
      mode: 'guide',
      artifact_part_id: null,
      created_at: '2026-08-20T10:00:00Z',
    },
    [`/api/sessions/${SESSION_ID}/messages`]: MESSAGES,
    [`/api/sessions/${TWIN_SESSION_ID}`]: TWIN_SESSION,
    [`/api/sessions/${TWIN_SESSION_ID}/messages`]: TWIN_MESSAGES,
    '/api/settings': SETTINGS,
    '/api/desktop-import/status': { available: false, status: 'idle' },
    '/api/export/availability': { available: false, message: 'Not on this machine.' },
    '/api/solutions/1/segmentation': SOLUTION_DETAIL,
    [`/api/solutions/${SOLUTION_ID}`]: SOLUTION_DETAIL,
    [`/api/solutions/${SOLUTION_ID}/status`]: { parts: [] },
    '/api/drafts': [],
    [`/api/drafts/${DRAFT_ID}`]: DRAFT_DETAIL,
    [`/api/drafts/${DRAFT_ID}/status`]: STATUS,
    [`/api/drafts/${DRAFT_ID}/pending`]: null,
    [`/api/drafts/${DRAFT_ID}/brief`]: null,
    [`/api/drafts/${DRAFT_ID}/plan`]: null,
    [`/api/drafts/${DRAFT_ID}/sessions`]: [
      {
        id: DRAFT_SESSION_ID,
        class_id: CLASS_ID,
        title: 'Draft questions',
        mode: 'guide',
        artifact_part_id: 40,
        created_at: '2026-08-21T09:00:00Z',
      },
    ],
    [`/api/drafts/${DRAFT_ID}/comments`]: DRAFT_COMMENTS,
    [`/api/drafts/${DRAFT_ID}/live-suggestion`]: null,
    ...extra,
  }
  const responders = new Map<string, Responder>()
  for (const [path, body] of Object.entries(handlers)) {
    responders.set(path, (route) => void json(route, body))
  }

  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    apiCalls.push(path)
    // The live store wins for its session: the pane reads the conversation as it grows.
    if (live && liveRows !== null && path === `/api/sessions/${live.sessionId}/messages`) {
      liveServed = [...liveRows]
      return json(route, liveRows)
    }
    const responder = responders.get(path)
    if (responder) return responder(route)
    if (path.endsWith('/messages')) {
      return json(route, MESSAGES)
    }
    return json(route, [])
  })
  return {
    apiCalls,
    appendMessages:
      liveRows === null ? undefined : (rows: unknown[]) => void liveRows.push(...rows),
    servedMessages: liveRows === null ? undefined : () => liveServed ?? [],
  }
}

export async function setTheme(page: Page, theme: 'light' | 'dark') {
  await page.addInitScript((value) => localStorage.setItem('lyra-theme', value), theme)
}
