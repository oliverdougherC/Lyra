import { mkdir } from 'node:fs/promises'
import { createServer, type ServerResponse } from 'node:http'
import { expect, test, type Page } from '@playwright/test'

import {
  CLASS_ID,
  MESSAGES,
  SESSION_ID,
  TWIN_SESSION_ID,
  installLyraApi,
} from './pla-504-math-fixture'

const activityPath = `/api/classes/${CLASS_ID}/sessions/${SESSION_ID}/agent/activity`
const recoveredEvents = [
  {
    id: 'bad-arguments',
    tool: 'read_file',
    capability: 'workspace_read',
    effect: 'read',
    state: 'failed',
    target_kind: 'file',
    target_id: 'missing.py',
    error_message: 'Invalid path',
    started_at: '2026-08-20T10:01:00Z',
    finished_at: '2026-08-20T10:01:01Z',
    result_summary: null,
  },
  {
    id: 'correct-arguments',
    tool: 'read_file',
    capability: 'workspace_read',
    effect: 'read',
    state: 'succeeded',
    target_kind: 'file',
    target_id: 'main.py',
    error_message: null,
    started_at: '2026-08-20T10:01:02Z',
    finished_at: '2026-08-20T10:01:03Z',
    result_summary: null,
  },
]

async function geometry(page: Page) {
  return page.evaluate(() => {
    const input = document.querySelector<HTMLTextAreaElement>(
      'textarea[aria-label="Message Lyra"]',
    )!
    const hosts = document.querySelectorAll<HTMLElement>('[data-native-chat-host]')
    const fallbacks = document.querySelectorAll<HTMLElement>(
      '[role="region"][aria-label="Conversation"]',
    )
    if (hosts.length !== 1 || fallbacks.length > 1) {
      throw new Error(
        `Expected one conversation host and at most one fallback, found ${hosts.length}/${fallbacks.length}`,
      )
    }
    const host = hosts[0]!
    const viewport = fallbacks[0] ?? host
    if (!host.parentElement?.contains(viewport))
      throw new Error('Measured surface is not the conversation')
    const composer = input.getBoundingClientRect()
    const reader = viewport.getBoundingClientRect()
    return {
      target: fallbacks.length ? 'conversation-dom-fallback' : 'conversation-native-host',
      transcriptTop: reader.top,
      composerTop: composer.top,
      viewportTop: reader.top,
      viewportBottom: reader.bottom,
      scrollTop: viewport.scrollTop,
    }
  })
}

function gate() {
  let release!: () => void
  const pending = new Promise<void>((resolve) => {
    release = resolve
  })
  return { pending, release }
}

function writeFrame(response: ServerResponse, frame: unknown) {
  response.write(`data: ${JSON.stringify(frame)}\n\n`)
}

const createdSessionId = 33
const question = 'Check the first step of this synthetic example.'
const answer = 'The first step is to identify the denominator. '.repeat(90)

for (const variant of [
  { name: 'mac-minimum', width: 800, height: 600, zoom: 1 },
  { name: 'mac-large-text', width: 900, height: 650, zoom: 1.25 },
]) {
  test(`new draft through tool, stream, completion and history: ${variant.name}`, async ({
    page,
  }) => {
    const firstTool = gate()
    const startStream = gate()
    const firstText = gate()
    const finish = gate()
    const publishHistory = gate()
    const rows: unknown[] = []
    let created = false
    const session = {
      id: createdSessionId,
      class_id: CLASS_ID,
      title: 'Synthetic first turn',
      mode: 'guide',
      artifact_part_id: null,
      created_at: '2026-08-20T10:00:00Z',
    }
    const server = createServer((request, response) => {
      const cors = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
        'Access-Control-Allow-Headers': 'content-type, x-lyra-session',
      }
      if (request.method === 'OPTIONS') {
        response.writeHead(204, cors)
        response.end()
        return
      }
      if (
        request.method === 'POST' &&
        request.url === `/api/classes/${CLASS_ID}/sessions/${createdSessionId}/agent-chat`
      ) {
        response.writeHead(200, {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          'Connection': 'keep-alive',
          ...cors,
        })
        response.flushHeaders()
        void (async () => {
          await new Promise((resolve) => setTimeout(resolve, 150))
          writeFrame(response, {
            type: 'activity',
            activity: {
              audit_id: 'first-tool',
              tool: 'read_file',
              capability: 'workspace_read',
              effect: 'read',
              state: 'succeeded',
              target_kind: 'file',
              target_id: 'synthetic.txt',
            },
          })
          firstTool.release()
          await startStream.pending
          writeFrame(response, { type: 'token', text: answer.slice(0, 3000) })
          firstText.release()
          await finish.pending
          writeFrame(response, { type: 'token', text: answer.slice(3000) })
          rows.push(
            {
              id: 331,
              session_id: createdSessionId,
              role: 'user',
              content: question,
              thinking: '',
              thinking_ms: 0,
              retrieval_trimmed: false,
              omitted_document_count: 0,
              tool_activity: [],
              created_at: '2026-08-20T10:00:01Z',
            },
            {
              id: 332,
              session_id: createdSessionId,
              role: 'assistant',
              content: answer,
              thinking: '',
              thinking_ms: 0,
              retrieval_trimmed: false,
              omitted_document_count: 0,
              tool_activity: [],
              created_at: '2026-08-20T10:00:02Z',
            },
          )
          writeFrame(response, {
            type: 'result',
            result: {
              message_id: 332,
              content: answer,
              stopped: 'completed',
              detail: '',
              activity: [],
              source_ids: [],
              workspace_change_ids: [],
              command_request_ids: [],
              profile_fact_ids: [],
            },
          })
          response.end()
        })()
        return
      }
      response.writeHead(200, { 'Content-Type': 'application/json', ...cors })
      response.end('[]')
    })
    await new Promise<void>((resolve, reject) => {
      server.once('error', reject)
      server.listen(8000, '127.0.0.1', resolve)
    })
    try {
      await page.setViewportSize({ width: variant.width, height: variant.height })
      await page.emulateMedia({ reducedMotion: 'reduce' })
      await installLyraApi(
        page,
        {
          [`/api/classes/${CLASS_ID}/sessions`]: [],
          [`/api/sessions/${createdSessionId}`]: session,
          [`/api/classes/${CLASS_ID}/sessions/${createdSessionId}/agent/access-dismissals`]: {
            dismissals: [],
          },
        },
        { sessionId: createdSessionId, initial: rows },
      )
      await page.route(`**/api/sessions/${createdSessionId}/messages`, (route) =>
        route.fulfill({ json: rows }),
      )
      await page.route(`**/api/classes/${CLASS_ID}/sessions`, (route) =>
        route.request().method() === 'POST'
          ? ((created = true), route.fulfill({ json: session }))
          : route.fulfill({ json: created ? [session] : [] }),
      )
      await page.route(
        `**/api/classes/${CLASS_ID}/sessions/${createdSessionId}/agent/activity`,
        async (route) => {
          await publishHistory.pending
          await route.fulfill({ json: recoveredEvents })
        },
      )
      await page.route(
        `**/api/classes/${CLASS_ID}/sessions/${createdSessionId}/agent-chat`,
        (route) => route.continue(),
      )
      await page.goto(`/#/classes/${CLASS_ID}/chat?session=new`)
      await expect(page.getByRole('textbox', { name: 'Message Lyra' })).toBeVisible()
      await page.evaluate((zoom) => {
        document.documentElement.style.zoom = String(zoom)
      }, variant.zoom)
      const draft = await geometry(page)
      expect(draft.target).toBe('conversation-dom-fallback')
      await page.getByRole('textbox', { name: 'Message Lyra' }).fill(question)
      await page.getByRole('button', { name: 'Send message' }).click()
      await expect(page).toHaveURL(new RegExp(`session=${createdSessionId}`))
      await firstTool.pending
      const tool = await geometry(page)
      expect(tool.target).toBe('conversation-dom-fallback')
      startStream.release()
      await firstText.pending
      await expect(page.locator('.assistant-content').last()).toContainText('first step')
      const streaming = await geometry(page)
      expect(streaming.target).toBe(tool.target)
      // A real user scroll gesture detaches following while answer text continues.
      await expect
        .poll(() =>
          page
            .getByRole('region', { name: 'Conversation' })
            .evaluate((node) => node.scrollHeight - node.clientHeight),
        )
        .toBeGreaterThan(100)
      const scrollable = await page
        .getByRole('region', { name: 'Conversation' })
        .evaluate((node) => {
          const hasOverflow = node.scrollHeight > node.clientHeight
          node.dispatchEvent(new WheelEvent('wheel', { deltaY: -120, bubbles: true }))
          node.scrollTop = 0
          return hasOverflow
        })
      expect(scrollable, 'the manual scroll-away did not have overflowing content').toBe(true)
      await expect(page.getByRole('button', { name: 'Jump to latest' })).toBeVisible()
      finish.release()
      await expect(page.locator('.assistant-content').last()).toContainText('denominator')
      await expect(page.getByRole('button', { name: 'Try this answer again' })).toBeVisible()
      await page.evaluate(async () => {
        await document.fonts.ready
        await new Promise<void>((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
        )
      })
      await expect(page.getByRole('button', { name: 'Activity history' })).toHaveCount(0)
      const before = await geometry(page)
      expect(before.scrollTop, 'completion pulled the reader back to the tail').toBeLessThanOrEqual(
        0.5,
      )
      publishHistory.release()
      await expect(page.getByRole('button', { name: 'Activity history' })).toBeVisible()
      const after = await geometry(page)
      expect(after.target).toBe(before.target)
      for (const key of [
        'transcriptTop',
        'composerTop',
        'viewportTop',
        'viewportBottom',
        'scrollTop',
      ] as const) {
        expect(
          Math.abs(after[key] - before[key]),
          `${key} changed on automatic history availability`,
        ).toBeLessThanOrEqual(0.5)
      }
      await expect(page.getByText(/needs attention/)).toHaveCount(0)
      await page.getByRole('button', { name: 'Activity history' }).click()
      await expect(page.getByText('Invalid path')).toBeVisible()
      // The deliberate disclosure is allowed to use space. A session refetch remains available.
      await page.reload()
      await expect(page.getByRole('button', { name: 'Activity history' })).toBeVisible()
    } finally {
      startStream.release()
      finish.release()
      publishHistory.release()
      await new Promise<void>((resolve) => server.close(() => resolve()))
    }
  })
}

test('attention actions use registered menu behavior and retain keyboard focus', async ({
  page,
}) => {
  await page.setViewportSize({ width: 800, height: 600 })
  const commands = Array.from({ length: 12 }, (_, index) => 71 + index).map((id) => ({
    id,
    workspace_id: 51,
    session_id: SESSION_ID,
    argv: ['python3', `test_${id}.py`],
    relative_cwd: '.',
    reason: `Verify synthetic case ${id}.`,
    expected_signal: 'tests passed',
    timeout_seconds: 60,
    state: 'pending',
    confirmed_at: null,
    exit_code: null,
    stdout_text: null,
    stderr_text: null,
    truncated: false,
  }))
  await installLyraApi(page, {
    [`/api/classes/${CLASS_ID}/workspace`]: {
      id: 51,
      class_id: CLASS_ID,
      root_path: '/tmp/lyra-fixture',
      display_name: 'Fixture',
      read_enabled: true,
      change_proposals_enabled: true,
      commands_enabled: true,
      created_at: '2026-08-20T09:00:00Z',
      updated_at: '2026-08-20T09:00:00Z',
    },
    [`/api/classes/${CLASS_ID}/sessions/${SESSION_ID}/workspace/commands`]: commands,
    [`/api/classes/${CLASS_ID}/sessions/${SESSION_ID}/agent/access-dismissals`]: { dismissals: [] },
    [`/api/sessions/${SESSION_ID}/messages`]: MESSAGES,
  })
  await page.goto(`/#/classes/${CLASS_ID}/chat?session=${SESSION_ID}`)
  const trigger = page.getByRole('button', { name: /12 items need attention/i })
  await expect(trigger).toBeVisible()
  const before = await geometry(page)
  await trigger.focus()
  await page.keyboard.press('Enter')
  const menu = page.getByRole('menu')
  await expect(menu).toBeVisible()
  const bounds = await page.evaluate(() => {
    const menu = document.querySelector<HTMLElement>('[role="menu"][data-state="open"]')!
    const conversation = document.querySelector<HTMLElement>(
      '[role="region"][aria-label="Conversation"]',
    )!
    const a = menu.getBoundingClientRect()
    const b = conversation.getBoundingClientRect()
    return {
      overlaps: a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top,
      menu: { left: a.left, right: a.right, top: a.top, bottom: a.bottom },
      conversation: { left: b.left, right: b.right, top: b.top, bottom: b.bottom },
    }
  })
  expect(bounds.menu.bottom).toBeGreaterThan(bounds.menu.top)
  // In this crowded supported browser case the menu stays above chat. If layout or content
  // makes it overlap later, its truthful role registers it with the native occlusion observer.
  expect(bounds.overlaps).toBe(false)
  expect(menu).toHaveAttribute('data-state', 'open')
  await page.keyboard.press('Escape')
  await expect(menu).toHaveCount(0)
  await expect(trigger).toBeFocused()
  expect(Math.abs((await geometry(page)).scrollTop - before.scrollTop)).toBeLessThanOrEqual(0.5)
  await trigger.click()
  await expect(menu).toBeVisible()
  await page.mouse.click(10, before.viewportTop + 10)
  await expect(menu).toHaveCount(0)
  await trigger.focus()
  await page.keyboard.press('Enter')
  await page.getByRole('menuitem', { name: /test_72.py/ }).focus()
  await page.keyboard.press('Enter')
  await expect(page.locator('[data-attention-id="command:72"]')).toBeFocused()
})

for (const variant of [
  { name: 'wide', width: 900, height: 650, zoom: 1 },
  { name: 'narrow-large-text', width: 390, height: 560, zoom: 1.25 },
])
  test(`history availability leaves transcript and composer geometry unchanged: ${variant.name}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: variant.width, height: variant.height })
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await installLyraApi(page, {
      [`/api/classes/${CLASS_ID}/sessions/${SESSION_ID}/agent/access-dismissals`]: {
        dismissals: [],
      },
    })
    let release!: () => void
    const pending = new Promise<void>((resolve) => {
      release = resolve
    })
    await page.route(`**${activityPath}`, async (route) => {
      await pending
      await route.fulfill({ json: recoveredEvents })
    })
    await page.goto(`/#/classes/${CLASS_ID}/chat?session=${SESSION_ID}`)
    await expect(page.getByRole('textbox', { name: 'Message Lyra' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Activity history' })).toHaveCount(0)
    await page.evaluate((zoom) => {
      document.documentElement.style.zoom = String(zoom)
    }, variant.zoom)
    await page.evaluate(async () => {
      await document.fonts.ready
      await new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      )
      const viewport = document.querySelector<HTMLElement>(
        '[role="region"][aria-label="Conversation"]',
      )
      if (viewport) viewport.scrollTop = 0
    })
    const before = await geometry(page)
    expect(before.target).toBe('conversation-dom-fallback')
    await mkdir('test-results', { recursive: true })
    await page.screenshot({ path: `test-results/activity-before-${variant.name}.png` })

    release()
    await expect(page.getByRole('button', { name: 'Activity history' })).toBeVisible()
    const after = await geometry(page)
    expect(after.target).toBe(before.target)
    await page.screenshot({ path: `test-results/activity-after-${variant.name}.png` })
    for (const key of [
      'transcriptTop',
      'composerTop',
      'viewportTop',
      'viewportBottom',
      'scrollTop',
    ] as const) {
      expect(
        Math.abs(after[key] - before[key]),
        `${key} changed on history availability`,
      ).toBeLessThanOrEqual(0.5)
    }
    await expect(page.getByText(/needs attention/)).toHaveCount(0)
    await page.getByRole('button', { name: 'Activity history' }).click()
    await expect(page.getByText('Invalid path')).toBeVisible()
    await page.reload()
    await expect(page.getByRole('button', { name: 'Activity history' })).toBeVisible()
    await expect(page.getByText(/needs attention/)).toHaveCount(0)
    await page.goto(`/#/classes/${CLASS_ID}/chat?session=${TWIN_SESSION_ID}`)
    await expect(page.getByRole('textbox', { name: 'Message Lyra' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Activity history' })).toHaveCount(0)
    await page.goto(`/#/classes/${CLASS_ID}/chat?session=${SESSION_ID}`)
    await expect(page.getByRole('button', { name: 'Activity history' })).toBeVisible()
    await expect(page.getByText(/needs attention/)).toHaveCount(0)
  })
