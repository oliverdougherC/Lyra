import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * The narrow-window contract (PLA-401, workstream B).
 *
 * Lyra is meant to run beside a code editor, a document, and a browser. A 13-inch laptop
 * with Lyra in a slice of the screen is a first-class use case, so the layout is measured
 * against the available task space rather than against "desktop vs phone". At every size in
 * the matrix the page must not scroll sideways, the primary task must stay visible, the
 * composer must keep a usable height, and navigation may not permanently consume about a
 * third of the window.
 */

const CLASS_ID = 12
const SESSION_ID = 4

/** The one structural line of the shell: below it the rail is a temporary sheet. */
const RAIL_BREAKPOINT = 1024
/** A docked rail may take at most this share of the window; a third is the line we cross. */
const RAIL_MAX_FRACTION = 0.34

const CLASS_12 = {
  id: CLASS_ID,
  name: 'Continuous-Time Signals',
  code: 'ECE 203',
  semester: 'Fall 2026',
  archived: false,
  document_count: 2,
  created_at: '2026-08-01T09:00:00Z',
  last_active_at: '2026-08-30T18:15:00Z',
}

const OTHER_CLASS = {
  id: 1,
  name: 'Linear Algebra',
  code: 'MATH 220',
  semester: 'Fall 2026',
  archived: false,
  document_count: 6,
  created_at: '2026-08-01T09:00:00Z',
  last_active_at: '2026-08-29T11:00:00Z',
}

const DOCUMENTS = [
  {
    id: 101,
    class_id: CLASS_ID,
    filename: 'Syllabus.pdf',
    mime: 'application/pdf',
    byte_size: 245_760,
    state: 'ready',
    stage_detail: null,
    pages_total: 4,
    pages_done: 4,
    pages_skipped: 0,
    pages_failed: 0,
    recognize: false,
    error_message: null,
    created_at: '2026-08-01T09:05:00Z',
  },
  {
    id: 102,
    class_id: CLASS_ID,
    filename: 'Lecture 5 - Laplace Transforms.pdf',
    mime: 'application/pdf',
    byte_size: 1_800_000,
    state: 'ready',
    stage_detail: null,
    pages_total: 22,
    pages_done: 22,
    pages_skipped: 0,
    pages_failed: 0,
    recognize: false,
    error_message: null,
    created_at: '2026-08-12T14:20:00Z',
  },
]

const SESSIONS = [
  {
    id: SESSION_ID,
    class_id: CLASS_ID,
    title: 'Convolution and LTI systems',
    mode: 'guide',
    artifact_part_id: null,
    created_at: '2026-08-20T10:00:00Z',
  },
]

const MESSAGES = [
  {
    id: 1,
    session_id: SESSION_ID,
    role: 'user',
    content: 'What does it mean for a system to be stable?',
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
    content:
      'A linear time-invariant system is BIBO stable when every bounded input produces a bounded output. For a continuous-time system that holds exactly when the impulse response is absolutely integrable over all time.',
    thinking: '',
    thinking_ms: 0,
    retrieval_trimmed: false,
    omitted_document_count: 2,
    tool_activity: [],
    created_at: '2026-08-20T10:01:20Z',
  },
]
// The contextual agent work in the same conversation: an attached workspace (read not yet
// granted, so one just-in-time request card is live), a pending edit, and a pending
// verification command. This is the fullest live-work state the work surface takes, and
// it is what the narrow window must hold without sideways scroll or a displaced composer.
const WORKSPACE = {
  id: 51,
  class_id: CLASS_ID,
  root_path: '/tmp/lyra-cs201',
  display_name: 'cs201-homework-4',
  read_enabled: false,
  change_proposals_enabled: true,
  commands_enabled: true,
  created_at: '2026-08-20T09:00:00Z',
  updated_at: '2026-08-20T09:00:00Z',
}

const AGENT_ACTIVITY = [
  {
    id: 'ev-agent-1',
    tool: 'request_workspace_access',
    capability: 'access_request',
    effect: 'pure',
    state: 'succeeded',
    target_kind: 'capability_request',
    target_id: 'read',
    error_message: null,
    started_at: '2026-08-20T09:50:00Z',
    finished_at: '2026-08-20T09:50:01Z',
    result_summary: {
      scope: 'read',
      reason: 'To inspect this homework, Lyra needs to read the files in cs201-homework-4.',
    },
  },
]

const AGENT_DISMISSALS = { dismissals: [] }

const AGENT_CHANGE = {
  id: 61,
  workspace_id: 51,
  session_id: SESSION_ID,
  path: 'parser.py',
  rationale: 'Add the parser skeleton.',
  state: 'pending',
  current_hash: 'a'.repeat(64),
  current_content: 'def parse(text):\n    raise NotImplementedError\n',
  proposed_content: 'def parse(text):\n    return text.split()\n',
  hunks: [
    {
      index: 0,
      hash: 'b'.repeat(64),
      lines: ['-    raise NotImplementedError', '+    return text.split()'],
    },
  ],
  created_at: '2026-08-20T09:55:00Z',
  updated_at: '2026-08-20T09:55:00Z',
}

const AGENT_COMMAND = {
  id: 71,
  workspace_id: 51,
  session_id: SESSION_ID,
  argv: ['python3', 'test_parser.py'],
  relative_cwd: '.',
  reason: 'Verify the parser skeleton against its test.',
  expected_signal: 'tests passed',
  timeout_seconds: 60,
  state: 'pending',
  confirmed_at: null,
  exit_code: null,
  stdout_text: null,
  stderr_text: null,
  truncated: false,
}

const SETTINGS = {
  endpoint_url: 'http://127.0.0.1:8080/v1',
  api_key_set: false,
  api_key_storage: 'file',
  model: null,
  context_window: 8192,
  extraction_enabled: true,
  remote_ack: false,
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
}

const IMPORT_IDLE = {
  available: false,
  destination_ready: false,
  status: 'idle',
  phase: null,
  message: null,
  source_name: null,
  copied_entries: 0,
  total_entries: 0,
  copied_bytes: 0,
  total_bytes: 0,
  cancel_requested: false,
  can_resume: false,
  requires_restart: false,
  preview: null,
}

// One of every kind the Work tab aggregates, so the filter rows have something real to
// swap in and the all-list is a genuine getting-back-to-it list.
const SOLUTIONS = [
  {
    id: 21,
    class_id: CLASS_ID,
    title: 'Homework 2',
    state: 'ready',
    stage_detail: null,
    problems_total: 4,
    problems_done: 4,
    error_message: null,
    created_at: '2026-08-05 08:00:00',
    updated_at: '2026-08-05 09:30:00',
    sources: [{ document_id: 101, filename: 'Syllabus.pdf', role: 'problem_set' }],
  },
]

const DRAFTS = [
  {
    id: 31,
    class_id: CLASS_ID,
    title: 'First draft of the essay',
    state: 'ready',
    stage_detail: null,
    problems_total: null,
    problems_done: 0,
    error_message: null,
    created_at: '2026-08-06 09:00:00',
    updated_at: '2026-08-06 09:30:00',
  },
]

const STUDY = {
  decks: [
    {
      id: 41,
      class_id: CLASS_ID,
      kind: 'flashcard_deck',
      title: 'Laplace transforms deck',
      state: 'ready',
      stage_detail: null,
      problems_total: null,
      problems_done: 0,
      error_message: null,
      created_at: '2026-08-02 10:00:00',
      updated_at: '2026-08-07 10:00:00',
      cards_total: 24,
      due_count: 5,
      buckets: { new: 19, learning: 3, mastered: 2 },
    },
  ],
  quizzes: [
    {
      id: 42,
      class_id: CLASS_ID,
      kind: 'quiz',
      title: 'Week 5 quiz',
      state: 'ready',
      stage_detail: null,
      problems_total: 6,
      problems_done: 2,
      error_message: null,
      created_at: '2026-08-03 10:00:00',
      updated_at: '2026-08-04 10:00:00',
    },
  ],
}

// Two facts Lyra is not sure about, one it is sure of: only the first two still need
// the student, and that is exactly what the hub's chip counts.
const PROFILE = {
  facts: [
    {
      id: 51,
      class_id: CLASS_ID,
      kind: 'deadline',
      label: 'Quiz 5 due',
      value: 'Aug 12',
      confidence: 'low',
      confirmed: false,
      rejected: false,
      edited: false,
      source_document_id: 101,
      source_filename: 'Syllabus.pdf',
      sources: ['Syllabus.pdf'],
      source_writer_id: null,
      source_excerpt_id: null,
      source_title: null,
      source_url: null,
      created_at: '2026-08-01 09:10:00',
    },
    {
      id: 52,
      class_id: CLASS_ID,
      kind: 'professor',
      label: 'Professor',
      value: 'Prof. Alvarez',
      confidence: 'low',
      confirmed: false,
      rejected: false,
      edited: false,
      source_document_id: 101,
      source_filename: 'Syllabus.pdf',
      sources: ['Syllabus.pdf'],
      source_writer_id: null,
      source_excerpt_id: null,
      source_title: null,
      source_url: null,
      created_at: '2026-08-01 09:10:00',
    },
    {
      id: 53,
      class_id: CLASS_ID,
      kind: 'topic',
      label: 'Signal stability',
      value: 'BIBO stability',
      confidence: 'high',
      confirmed: false,
      rejected: false,
      edited: false,
      source_document_id: 102,
      source_filename: 'Lecture 5 - Laplace Transforms.pdf',
      sources: ['Lecture 5 - Laplace Transforms.pdf'],
      source_writer_id: null,
      source_excerpt_id: null,
      source_title: null,
      source_url: null,
      created_at: '2026-08-12 14:25:00',
    },
  ],
  extraction_skipped_reason: null,
}

/**
 * A class populated enough that the hub and the chat render with real content: two ready
 * documents, one conversation, one answer pair. Unknown reads under the class tree fall
 * back to an empty list rather than an error, so the shell stays calm instead of surfacing
 * a red state the matrix is not testing.
 */
export async function installClassMocks(page: Page) {
  const json = (body: unknown) => ({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
  const handlers: Record<string, unknown> = {
    '/api/classes': [CLASS_12, OTHER_CLASS],
    [`/api/classes/${CLASS_ID}`]: CLASS_12,
    [`/api/classes/${CLASS_ID}/documents`]: DOCUMENTS,
    [`/api/classes/${CLASS_ID}/sessions`]: SESSIONS,
    [`/api/classes/${CLASS_ID}/solutions`]: SOLUTIONS,
    [`/api/classes/${CLASS_ID}/study`]: STUDY,
    [`/api/classes/${CLASS_ID}/drafts`]: DRAFTS,
    [`/api/classes/${CLASS_ID}/profile`]: PROFILE,
    [`/api/sessions/${SESSION_ID}/messages`]: MESSAGES,
    [`/api/classes/${CLASS_ID}/workspace`]: WORKSPACE,
    [`/api/classes/${CLASS_ID}/sessions/${SESSION_ID}/agent/activity`]: AGENT_ACTIVITY,
    [`/api/classes/${CLASS_ID}/sessions/${SESSION_ID}/agent/access-dismissals`]: AGENT_DISMISSALS,
    [`/api/classes/${CLASS_ID}/sessions/${SESSION_ID}/workspace/changes`]: [AGENT_CHANGE],
    [`/api/classes/${CLASS_ID}/sessions/${SESSION_ID}/workspace/commands`]: [AGENT_COMMAND],
    '/api/settings': SETTINGS,
    '/api/desktop-import/status': IMPORT_IDLE,
  }

  await page.route('http://127.0.0.1:8000/api/**', async (route: Route) => {
    const pathname = new URL(route.request().url()).pathname
    if (pathname in handlers) {
      await route.fulfill(json(handlers[pathname]))
      return
    }
    await route.fulfill(json([]))
  })
}

interface Measurement {
  innerWidth: number
  innerHeight: number
  scrollWidth: number
  railVisible: boolean
  railWidth: number
}

async function measure(page: Page): Promise<Measurement> {
  return page.evaluate(() => {
    const rail = document.querySelector('[data-slot="sidebar-container"]') as HTMLElement | null
    let railVisible = false
    let railWidth = 0
    if (rail) {
      const rect = rail.getBoundingClientRect()
      const shown = getComputedStyle(rail).display !== 'none' && rect.width > 0
      railVisible = shown
      railWidth = shown ? rect.width : 0
    }
    return {
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      scrollWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
      railVisible,
      railWidth,
    }
  })
}

/**
 * Below the rail breakpoint the navigation is a temporary sheet, so it must not be docked
 * and eating the window. At and above it the rail docks at 16rem, which is at most a
 * quarter of the work surface and never a third.
 */
function expectNavigationFits(measurement: Measurement, width: number) {
  if (width < RAIL_BREAKPOINT) {
    expect(
      measurement.railVisible,
      `a docked rail is visible at ${width}px; it should be a sheet`,
    ).toBe(false)
  } else {
    expect(
      measurement.railWidth / measurement.innerWidth,
      `the docked rail takes ${(100 * measurement.railWidth) / measurement.innerWidth}% of a ${width}px window`,
    ).toBeLessThanOrEqual(RAIL_MAX_FRACTION)
  }
}

const MATRIX = [
  { width: 540, height: 720 },
  { width: 640, height: 760 },
  { width: 768, height: 700 },
  { width: 900, height: 700 },
  { width: 1024, height: 768 },
  { width: 1280, height: 800 },
]

for (const { width, height } of MATRIX) {
  test(`${width}x${height}: the hub and the chat keep the task in the window`, async ({ page }) => {
    const pageErrors: string[] = []
    page.on('pageerror', (error) => pageErrors.push(error.message))
    await installClassMocks(page)
    await page.setViewportSize({ width, height })

    // The class hub: where a student decides what to work on. The front door and the
    // dominant practice path are the primary task and must both be on screen.
    await page.goto(`/#/classes/${CLASS_ID}`)
    await page.waitForLoadState('networkidle')
    await expect(page.getByRole('textbox', { name: /Ask about/ })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Practice now' })).toBeVisible()
    const hub = await measure(page)
    expect(
      hub.scrollWidth - hub.innerWidth,
      `the hub scrolls sideways at ${width}px`,
    ).toBeLessThanOrEqual(0)
    expectNavigationFits(hub, width)

    // The chat: the working surface. The conversation, the source chip, and the composer
    // must all fit; the composer keeps a usable height and stays inside the window.
    await page.goto(`/#/classes/${CLASS_ID}/chat?session=${SESSION_ID}`)
    await page.waitForLoadState('networkidle')
    const composer = page.locator('#message-composer')
    await expect(composer).toBeVisible()
    await expect(page.getByRole('button', { name: /Choose what Lyra reads/ })).toBeVisible()
    const chat = await measure(page)
    expect(
      chat.scrollWidth - chat.innerWidth,
      `the chat scrolls sideways at ${width}px`,
    ).toBeLessThanOrEqual(0)
    const box = await composer.boundingBox()
    expect(box, 'the composer has no box').not.toBeNull()
    expect(box!.height, 'the composer lost its usable height').toBeGreaterThanOrEqual(36)
    expect(box!.x + box!.width, 'the composer is clipped on the right').toBeLessThanOrEqual(
      width + 1,
    )
    expect(box!.y + box!.height, 'the composer is pushed out of the window').toBeLessThanOrEqual(
      height + 1,
    )
    // The context marks - the material scope and the folder attach - ride the input line as
    // compact marks, so the text entry keeps the majority of the row's width at every size
    // (PLA-403: the two wide labeled controls used to hand the row over at the left).
    const inputClaim = await page.evaluate(() => {
      const input = document.querySelector('#message-composer') as HTMLElement
      const well = input.closest('.rounded-2xl') as HTMLElement
      return input.getBoundingClientRect().width / well.getBoundingClientRect().width
    })
    expect(
      inputClaim,
      `the composer's context marks take over the input row at ${width}px`,
    ).toBeGreaterThanOrEqual(0.6)
    expectNavigationFits(chat, width)

    expect(pageErrors, 'a runtime error fired while the page held its layout').toEqual([])
  })
}

/**
 * Not every size in the matrix gets operated - 540x720, 768x700, and 1024x768 cover the
 * three ways a narrow window actually behaves: a temporary navigation sheet below the
 * rail line, a typical tablet slice, and the tightest docked-rail size. At each, the page
 * must be usable when operated, not merely unclipped when measured.
 */
const INTERACTION_MATRIX = [
  { width: 540, height: 720 },
  { width: 768, height: 700 },
  { width: 1024, height: 768 },
]

for (const { width, height } of INTERACTION_MATRIX) {
  test(`${width}x${height}: the narrow window stays usable when operated`, async ({ page }) => {
    const pageErrors: string[] = []
    page.on('pageerror', (error) => pageErrors.push(error.message))
    await installClassMocks(page)
    await page.setViewportSize({ width, height })

    const main = page.locator('#main-content')

    // The class hub: the Work filters drive the real route, and the class's unresolved
    // uncertainty keeps a compact home on the class itself. Everything here is scoped to
    // the page's main content: the docked rail carries its own links, and the sheet and
    // popover layers portal out of it.
    await page.goto(`/#/classes/${CLASS_ID}`)
    await page.waitForLoadState('networkidle')
    await expect(
      main.getByRole('button', { name: '2 class facts need confirmation' }),
    ).toBeVisible()

    const hash = () => page.evaluate(() => window.location.hash)
    await main.getByRole('tab', { name: /^Work/ }).click()
    await expect.poll(hash).toBe(`#/classes/${CLASS_ID}?tab=work`)

    // The all-list is the getting-back-to-it view: every kind the class holds.
    await expect(main.getByRole('link', { name: /Laplace transforms deck/ })).toBeVisible()
    await expect(main.getByRole('link', { name: /Week 5 quiz/ })).toBeVisible()
    await expect(main.getByRole('link', { name: /Convolution and LTI systems/ })).toBeVisible()

    // Each filter updates the actual hash route and swaps in its own list.
    await main.getByRole('tab', { name: 'Solutions', exact: true }).click()
    await expect.poll(hash).toBe(`#/classes/${CLASS_ID}?tab=work&work=solutions`)
    await expect(main.getByRole('link', { name: /Homework 2/ })).toBeVisible()
    await expect(main.getByRole('link', { name: /Convolution and LTI systems/ })).toBeHidden()

    await main.getByRole('tab', { name: 'Drafts', exact: true }).click()
    await expect.poll(hash).toBe(`#/classes/${CLASS_ID}?tab=work&work=drafts`)
    await expect(main.getByRole('link', { name: /First draft of the essay/ })).toBeVisible()
    await expect(main.getByRole('link', { name: /Homework 2/ })).toBeHidden()

    // A filtered URL is a real location: reloading it lands on the filtered view.
    await main.getByRole('tab', { name: 'Chats', exact: true }).click()
    await expect.poll(hash).toBe(`#/classes/${CLASS_ID}?tab=work&work=chats`)
    await page.reload()
    await page.waitForLoadState('networkidle')
    await expect(main.getByRole('tab', { name: 'Chats', exact: true })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    await expect(main.getByRole('link', { name: /Convolution and LTI systems/ })).toBeVisible()

    // "All" drops the param from the route and restores the combined list.
    await main.getByRole('tab', { name: 'All', exact: true }).click()
    await expect.poll(hash).toBe(`#/classes/${CLASS_ID}?tab=work`)
    await expect(main.getByRole('link', { name: /Laplace transforms deck/ })).toBeVisible()

    // The confirmation nudge opens the class details: it fits the window and closes.
    await page.getByRole('button', { name: '2 class facts need confirmation' }).click()
    const sheet = page.locator('[data-slot="sheet-content"]')
    await expect(sheet.getByRole('heading', { name: 'Class details' })).toBeVisible()
    await expect(sheet.getByText('Needs confirmation').first()).toBeVisible()
    const sheetBox = await sheet.boundingBox()
    expect(sheetBox, 'the sheet has no box').not.toBeNull()
    expect(sheetBox!.x, 'the sheet is clipped on the left').toBeGreaterThanOrEqual(0)
    expect(sheetBox!.x + sheetBox!.width, 'the sheet is clipped on the right').toBeLessThanOrEqual(
      width + 1,
    )
    expect(
      sheetBox!.y + sheetBox!.height,
      'the sheet is clipped at the bottom',
    ).toBeLessThanOrEqual(height + 1)
    await page.keyboard.press('Escape')
    await expect(sheet).toBeHidden()
    // The sheet is temporary: closing it hands focus back to the control that opened it.
    await expect(
      page.getByRole('button', { name: '2 class facts need confirmation' }),
    ).toBeFocused()

    // The chat: the source disclosure opens, is used, and closes with focus returning.
    await page.goto(`/#/classes/${CLASS_ID}/chat?session=${SESSION_ID}`)
    await page.waitForLoadState('networkidle')
    const chip = page.getByRole('button', { name: /Choose what Lyra reads/ })
    await expect(chip).toBeVisible()
    await chip.click()
    const disclosure = page.locator('[data-slot="popover-content"]')
    await expect(disclosure.getByText('What Lyra reads')).toBeVisible()
    await disclosure.getByRole('radio', { name: 'Lecture 5 - Laplace Transforms.pdf' }).click()
    // Choosing a document closes the disclosure, and the composer chip carries the choice.
    await expect(disclosure).toBeHidden()
    await expect(page.getByRole('button', { name: /Lyra reads only/ })).toBeVisible()
    await expect(chip).toBeFocused()

    // The composer keeps working: a typed question arms the send control.
    const composer = page.locator('#message-composer')
    await composer.click()
    await composer.pressSequentially('Is convolution commutative?')
    await expect(composer).toHaveValue('Is convolution commutative?')
    await expect(page.getByRole('button', { name: 'Send message' })).toBeEnabled()

    // Keyboard operation stays intact: Tab from the composer reaches the send control.
    await page.keyboard.press('Tab')
    await expect(page.getByRole('button', { name: 'Send message' })).toBeFocused()

    // The contextual agent work lives in the same conversation: the workspace chip, the
    // just-in-time access card, the pending edit, and the pending command all render in
    // the transcript and composer row without a second composer or any Agent destination.
    const workspaceChip = page.locator('[data-workspace-chip]')
    await expect(workspaceChip).toHaveText(/cs201-homework-4/)
    await expect(page.getByRole('button', { name: 'Attach a folder' })).toBeHidden()
    await expect(page.getByRole('button', { name: /Not now/ })).toHaveCount(1)
    const readRequest = page.locator('[data-access-request="read"]')
    await expect(readRequest.getByRole('button', { name: 'Approve' })).toBeVisible()
    await expect(
      readRequest.getByText(
        'To inspect this homework, Lyra needs to read the files in cs201-homework-4.',
      ),
    ).toBeVisible()
    await expect(page.locator('[aria-label="Workspace change for parser.py"]')).toBeVisible()
    await expect(page.locator('[aria-label="Command request 71"]')).toBeVisible()

    // Nothing on the page - the live work surfaces included - scrolls sideways, and the
    const surface = await measure(page)
    expect(
      surface.scrollWidth - surface.innerWidth,
      `the chat scrolls sideways at ${width}px with live agent work`,
    ).toBeLessThanOrEqual(0)
    // The diff review is the same kind of in-conversation card: it stays inside the window.
    const changeBox = await page
      .locator('[aria-label="Workspace change for parser.py"]')
      .boundingBox()
    expect(changeBox, 'the diff card has no box').not.toBeNull()
    expect(
      changeBox!.x + changeBox!.width,
      'the diff card is clipped on the right',
    ).toBeLessThanOrEqual(width + 1)
    const surfaceBox = await page.locator('[aria-label="Command request 71"]').boundingBox()
    expect(surfaceBox, 'the command card has no box').not.toBeNull()
    expect(
      surfaceBox!.x + surfaceBox!.width,
      'the command card is clipped on the right',
    ).toBeLessThanOrEqual(width + 1)
    const composerBox = await composer.boundingBox()
    expect(composerBox, 'the composer has no box').not.toBeNull()
    expect(
      composerBox!.y + composerBox!.height,
      'the live work pushed the composer out of the window',
    ).toBeLessThanOrEqual(height + 1)
    expect(
      composerBox!.x + composerBox!.width,
      'the composer is clipped on the right',
    ).toBeLessThanOrEqual(width + 1)
    // With the fullest context marks (a scoped document and an attached workspace, both
    // long-named), the marks truncate inside their caps instead of crowding the text: the
    // input keeps a typable width at the narrowest operated size (PLA-403).
    const inputWidth = await composer.evaluate((el) => el.getBoundingClientRect().width)
    expect(
      inputWidth,
      `the input lost its usable width at ${width}px with live context marks`,
    ).toBeGreaterThanOrEqual(140)

    // There is exactly one composer and no Agent destination: the bridge is gone, and the
    // answer stays the dominant surface in the transcript.
    expect(await page.locator('#message-composer').count()).toBe(1)
    expect(await page.getByRole('button', { name: 'Agent' }).count()).toBe(0)
    expect(await page.getByRole('tab', { name: 'Agent' }).count()).toBe(0)
    await expect(
      page.getByText(/A linear time-invariant system is BIBO stable/).first(),
    ).toBeVisible()

    // The workspace options menu is a temporary popover: it closes and restores focus.
    await page.getByRole('button', { name: 'Workspace options' }).click()
    await expect(page.getByRole('menuitem', { name: 'Detach workspace' })).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.getByRole('menuitem', { name: 'Detach workspace' })).toBeHidden()
    await expect(page.getByRole('button', { name: 'Workspace options' })).toBeFocused()

    expect(pageErrors, 'a runtime error fired while the window was operated').toEqual([])
  })
}
