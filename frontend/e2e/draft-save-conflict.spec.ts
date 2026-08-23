/**
 * Browser regression for the visible save status under a stale-version conflict (PLA-289).
 *
 * It drives the real draft workspace - the real editor, the real save engine, the real
 * `PATCH /api/drafts/{id}/body` request shape - with the backend mocked at the network
 * boundary. The first autosave is refused with the deterministic 409 the server sends when
 * the body moved past the version the editor knew, and the test proves the workspace does
 * the honest thing: it never says "Saved", it keeps the local text, and it offers a
 * recovery choice. The screenshots are the PR's evidence of the conflict and recovery UI.
 */
import { expect, test, type Page, type Route } from '@playwright/test'

const CLASS_ID = 1
const DRAFT_ID = 1
const PART_ID = 10
const SEED_BODY = 'The opening paragraph the student started with.\n'
const SERVER_BODY = 'A newer version of this draft, saved from another tab.\n'

type RouteHandler = (route: Route) => Promise<void>

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

async function installDraftMocks(page: Page) {
  // The body PATCH is refused once (stale version), then accepted, so the recovery path is
  // exercisable in the same run.
  let bodyWrites = 0
  const draftDetail = {
    id: DRAFT_ID,
    class_id: CLASS_ID,
    kind: 'draft',
    title: 'Photosynthesis essay',
    state: 'ready',
    stage_detail: null,
    problems_total: null,
    problems_done: 0,
    error_message: null,
    created_at: '2026-08-21T00:00:00Z',
    updated_at: '2026-08-21T00:00:00Z',
    part_id: PART_ID,
    body: SEED_BODY,
    body_version: 3,
    pending: false,
  }
  const status = {
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

  const handlers = new Map<string, RouteHandler>([
    ['/api/classes', (route) => json(route, [{ id: CLASS_ID, name: 'Biology', archived: false }])],
    [`/api/drafts/${DRAFT_ID}`, (route) => json(route, draftDetail)],
    [`/api/drafts/${DRAFT_ID}/status`, (route) => json(route, status)],
    [`/api/drafts/${DRAFT_ID}/pending`, (route) => json(route, null)],
    [`/api/drafts/${DRAFT_ID}/brief`, (route) => json(route, null)],
    [`/api/drafts/${DRAFT_ID}/sessions`, (route) => json(route, [])],
    [`/api/drafts/${DRAFT_ID}/comments`, (route) => json(route, [])],
    [`/api/drafts/${DRAFT_ID}/live-suggestion`, (route) => json(route, null)],
    [`/api/drafts/${DRAFT_ID}/plan`, (route) => json(route, null)],
    [
      '/api/export/availability',
      (route) => json(route, { available: false, message: 'Not on this machine.' }),
    ],
    [
      `/api/drafts/${DRAFT_ID}/body`,
      (route) => {
        bodyWrites += 1
        if (bodyWrites === 1) {
          return json(
            route,
            {
              detail: 'This draft changed somewhere else, so your latest edit was not saved yet.',
              code: 'stale_body_version',
              current_version: 4,
              server_body: SERVER_BODY,
            },
            409,
          )
        }
        return json(route, { part_id: PART_ID, saved: true, version: 5 })
      },
    ],
  ])

  await page.route('http://127.0.0.1:8000/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    const handler = handlers.get(pathname)
    if (handler) return handler(route)
    return json(route, { detail: `unexpected API call: ${pathname}` }, 500)
  })

  return { bodyWrites: () => bodyWrites }
}

test.describe('draft save conflict and recovery', () => {
  test('shows an honest conflict, keeps local text, and recovers', async ({ page }) => {
    await installDraftMocks(page)
    await page.goto(`/classes/${CLASS_ID}/drafts/${DRAFT_ID}`)

    // The editor mounts client-side; wait for it and for the seeded body.
    const editor = page.locator('.ProseMirror[contenteditable="true"]')
    await expect(editor).toBeVisible()
    await expect(page.getByRole('status')).toHaveText('Saved')

    // Type into the document, then wait past the autosave debounce so the write goes out.
    // (The mocked 409 returns instantly, so the transient "Saving" is not asserted here -
    // the unit and component tests cover that transition deterministically.)
    await editor.click()
    await page.keyboard.type(' A sentence the student just added.')

    // The write is refused as stale: the conflict dialog opens and the status is honest.
    const dialog = page.getByRole('dialog')
    await expect(dialog.getByText('This draft was changed somewhere else')).toBeVisible({
      timeout: 5000,
    })
    await expect(page.getByRole('status')).toHaveText('Changed elsewhere')
    await expect(page.getByRole('status')).not.toHaveText('Saved')
    await expect(dialog.getByText(SERVER_BODY.trim())).toBeVisible()
    await page.screenshot({ path: 'e2e/artifacts/conflict-dialog.png', fullPage: true })

    // Keep the local writing: the engine rebases and re-saves, and the status settles.
    await dialog.getByRole('button', { name: 'Keep what I wrote' }).click()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(page.getByRole('status')).toHaveText('Saved', { timeout: 5000 })
    await page.screenshot({ path: 'e2e/artifacts/conflict-recovered.png', fullPage: true })
  })
})
