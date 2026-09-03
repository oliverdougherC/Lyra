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

/**
 * A class populated enough that the hub and the chat render with real content: two ready
 * documents, one conversation, one answer pair. Unknown reads under the class tree fall
 * back to an empty list rather than an error, so the shell stays calm instead of surfacing
 * a red state the matrix is not testing.
 */
async function installClassMocks(page: Page) {
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
    [`/api/classes/${CLASS_ID}/solutions`]: [],
    [`/api/classes/${CLASS_ID}/study`]: { decks: [], quizzes: [] },
    [`/api/classes/${CLASS_ID}/drafts`]: [],
    [`/api/classes/${CLASS_ID}/profile`]: { facts: [], extraction_skipped_reason: null },
    [`/api/sessions/${SESSION_ID}/messages`]: MESSAGES,
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
  test(`${width}x${height}: the hub and the chat keep the task in the window`, async ({
    page,
  }) => {
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
    expectNavigationFits(chat, width)

    expect(pageErrors, 'a runtime error fired while the page held its layout').toEqual([])
  })
}
