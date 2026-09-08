/**
 * Real incremental streaming of the reveal cascade (PLA-499 / PLA-500, review round 2).
 *
 * The containment suite answers the agent-chat POST with one SSE body: the whole answer
 * arrives at once, and only the CSS-delayed cascade is left to observe. This suite feeds
 * the same endpoint from a real Node HTTP server whose chunks are delayed by fixed
 * amounts, so the source itself grows across frames while the assertions run against the
 * live page.
 *
 * The assertions are the review's invariants, read off the page as the stream runs:
 *
 *  - a span that has been revealed never re-hides at syntax closure — an emphasis that
 *    closes around its words, a span's line ending: the opacity of a completed unit
 *    stays 1 on every later sample;
 *  - pending deadlines are nondecreasing in reading order on every sample;
 *  - a display block keeps its container's rect: a `$$` block inside a list item sits
 *    inside that item's box;
 *  - one reveal layer: a unit's ancestors (list, item, blockquote, paragraph) never
 *    fade — the unit's own span, and nothing above it, is the reveal;
 *  - atomic visuals arrive whole: the code block is one unit with no unit inside it;
 *  - the turn settles onto the render of the same source, with the prices still prose,
 *    the digit-led mathematics still mathematics, and the unfinished equation printed
 *    literally.
 *
 * Screenshot files are captured at in-flight and settled checkpoints for inspection; the
 * test never decodes them.
 */
import { createServer, type Server } from 'node:http'
import { mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { expect, test, type Page } from '@playwright/test'

import {
  CLASS_ID,
  TWIN_SESSION,
  VISUAL_MESSAGE_ID,
  VISUAL_MESSAGES,
  VISUAL_SESSION,
  VISUAL_SESSION_ID,
  VISUAL_STREAM,
  installLyraApi,
} from './pla-504-math-fixture'

/** The app's baked API origin (VITE_API_BASE) — the real server must sit on it. */
const API_PORT = 8000
const visualUrl = `/#/classes/${CLASS_ID}/chat?session=${VISUAL_SESSION_ID}`

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * The deterministic feed: characters (2 at a time, 25 ms) for the first 60 characters,
 * words (up to the next space, 60 ms) until character 300, then bursts (40 characters,
 * 120 ms) to the end, and the terminal result frame 150 ms after the last token.
 */
function feedPlan(text: string): { chunk: string; delay: number }[] {
  const plan: { chunk: string; delay: number }[] = []
  let i = 0
  while (i < text.length) {
    if (i < 60) {
      const chunk = text.slice(i, i + 2)
      plan.push({ chunk, delay: 25 })
      i += chunk.length
    } else if (i < 300) {
      const space = text.indexOf(' ', i)
      const end = space === -1 ? text.length : space + 1
      plan.push({ chunk: text.slice(i, end), delay: 60 })
      i = end
    } else {
      const chunk = text.slice(i, i + 40)
      plan.push({ chunk, delay: 120 })
      i += chunk.length
    }
  }
  return plan
}

async function startFeedServer(text: string, messageId: number): Promise<Server> {
  const server = createServer((req, res) => {
    const agentChat = `/api/classes/${CLASS_ID}/sessions/${VISUAL_SESSION_ID}/agent-chat`
    const cors = {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
      'Access-Control-Allow-Headers': 'content-type, x-lyra-session',
    }
    // The page is served from the preview origin; the app's fetch is credential-free, so a
    // wildcard origin is enough. JSON bodies trigger a preflight.
    if (req.method === 'OPTIONS') {
      res.writeHead(204, cors)
      res.end()
      return
    }
    if (req.method === 'POST' && req.url === agentChat) {
      void (async () => {
        res.writeHead(200, {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          'Connection': 'keep-alive',
          ...cors,
        })
        for (const { chunk, delay } of feedPlan(text)) {
          res.write(`data: ${JSON.stringify({ type: 'token', text: chunk })}\n\n`)
          await sleep(delay)
        }
        await sleep(150)
        res.write(
          `data: ${JSON.stringify({
            type: 'result',
            result: {
              message_id: messageId,
              content: text,
              stopped: 'completed',
              detail: '',
              activity: [],
              source_ids: [],
              workspace_change_ids: [],
              command_request_ids: [],
              profile_fact_ids: [],
            },
          })}\n\n`,
        )
        res.end()
      })()
    } else {
      res.writeHead(200, { 'Content-Type': 'application/json', ...cors })
      res.end('[]')
    }
  })
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(API_PORT, '127.0.0.1', () => resolve())
  })
  return server
}

type UnitSample = {
  key: string
  at: number | null
  opacity: number
  tag: string
}

type Snapshot = {
  now: number
  units: UnitSample[]
  fadedAncestors: number
  displayInLi: boolean | null
  katexInline: number
  katexDisplay: number
  codeUnits: number
  codeNested: number
  text: string
  done: boolean
}

async function sample(page: Page): Promise<Snapshot> {
  return page.evaluate(() => {
    const root = Array.from(document.querySelectorAll<HTMLElement>('.assistant-content')).at(-1)!
    const units = Array.from(root.querySelectorAll<HTMLElement>('[data-stream-word]')).map((u) => ({
      key: u.dataset.streamWord ?? '',
      at: u.dataset.streamRevealAt ? Number(u.dataset.streamRevealAt) : null,
      opacity: Number(getComputedStyle(u).opacity),
      tag: u.tagName.toLowerCase(),
    }))
    const fadedAncestors = Array.from(root.querySelectorAll('li, ul, ol, blockquote, p')).filter(
      (el) => Number(getComputedStyle(el).opacity) < 0.999,
    ).length
    let displayInLi: boolean | null = null
    for (const display of root.querySelectorAll('.katex-display')) {
      const li = display.closest('li')
      if (!li) continue
      const a = li.getBoundingClientRect()
      const b = display.getBoundingClientRect()
      displayInLi = b.left >= a.left - 8 && b.right <= a.right + 8
    }
    return {
      now: performance.now(),
      units,
      fadedAncestors,
      displayInLi,
      katexInline: root
        ? Array.from(root.querySelectorAll('.katex:not(.katex-display)')).filter(
            (el) => !el.closest('.katex-display'),
          ).length
        : 0,
      katexDisplay: root ? root.querySelectorAll('.katex-display').length : 0,
      codeUnits: units.filter((u) => u.key.startsWith('code-')).length,
      codeNested: Array.from(root.querySelectorAll<HTMLElement>('[data-stream-word]')).filter(
        (u) => u.dataset.streamWord?.startsWith('code-') && u.querySelector('[data-stream-word]'),
      ).length,
      text: root ? (root.textContent ?? '') : '',
      done: units.length === 0,
    }
  })
}

test('a real delayed stream reveals in order and never re-hides', async ({ page }, testInfo) => {
  const server = await startFeedServer(VISUAL_STREAM, VISUAL_MESSAGE_ID)
  try {
    const shots = path.join(testInfo.outputDir, 'screens')
    mkdirSync(shots, { recursive: true })
    const shot = async (name: string) => {
      await page.screenshot({ path: path.join(shots, name) })
    }

    await installLyraApi(page, {
      [`/api/classes/${CLASS_ID}/sessions`]: [TWIN_SESSION, VISUAL_SESSION],
      [`/api/sessions/${VISUAL_SESSION_ID}`]: VISUAL_SESSION,
      [`/api/sessions/${VISUAL_SESSION_ID}/messages`]: VISUAL_MESSAGES,
      [`/api/classes/${CLASS_ID}/sessions/${VISUAL_SESSION_ID}/agent/access-dismissals`]: {
        dismissals: [],
      },
    })
    // Let the agent-chat POST pass through to the real server; every other API call stays
    // on the fixture.
    await page.route(
      `**/api/classes/${CLASS_ID}/sessions/${VISUAL_SESSION_ID}/agent-chat`,
      (route) => route.continue(),
    )

    await page.goto(visualUrl)
    await page.getByLabel('Message Lyra').fill('Stream the visual shapes.')
    await page.getByRole('button', { name: 'Send message' }).click()

    await expect(
      page.locator('.assistant-content').last().locator('[data-stream-word]').first(),
    ).toBeAttached()

    // While the stream runs: the in-flight invariants, sampled as the source grows.
    const firstSeen = new Map<string, number>()
    let shotEarly = false
    let shotDisplay = false
    const deadlineOf = new Map<string, number>()
    let samples = 0
    for (;;) {
      const snap = await sample(page)
      samples += 1
      for (const unit of snap.units) {
        if (unit.at === null) continue
        // A revealed unit (its animation complete) must keep its opacity: a re-hide would
        // be the visible span fading out at the moment its syntax closed.
        const previouslyVisible =
          (firstSeen.get(unit.key) ?? 0) >= 0.99 ||
          (unit.at + 220 <= snap.now ? firstSeen.get(unit.key) !== undefined : false)
        if (previouslyVisible && unit.opacity < 0.99) {
          throw new Error(`unit ${unit.key} re-hid at sample ${samples}: opacity ${unit.opacity}`)
        }
        if (unit.opacity > (firstSeen.get(unit.key) ?? 0)) firstSeen.set(unit.key, unit.opacity)
        deadlineOf.set(unit.key, unit.at)
      }
      // Pending deadlines stay nondecreasing in reading order.
      let prevAt = 0
      for (const unit of snap.units) {
        if (unit.at === null || unit.at <= snap.now) continue
        if (unit.at < prevAt) {
          throw new Error(
            `pending deadline out of reading order at sample ${samples}: ` +
              `a unit reveals at ${unit.at} ahead of ${prevAt}`,
          )
        }
        prevAt = unit.at
      }
      // One reveal layer: nothing above a unit fades; the marker is the color, not the item.
      expect(snap.fadedAncestors, `faded ancestors at sample ${samples}`).toBe(0)
      // The display block, once present, stays inside its item.
      if (snap.displayInLi === false) {
        throw new Error(`display block escaped its list item at sample ${samples}`)
      }
      // The atomic code block arrives as one unit, whole.
      if (snap.codeUnits > 0) {
        expect(snap.codeUnits, `code split at sample ${samples}`).toBe(1)
        expect(snap.codeNested, `nested unit inside the code at sample ${samples}`).toBe(0)
      }
      if (!shotEarly && snap.units.length > 3) {
        shotEarly = true
        await shot('in-flight-early.png')
      }
      if (!shotDisplay && snap.displayInLi === true) {
        shotDisplay = true
        await shot('in-flight-display.png')
      }
      if (snap.done) break
      await page.waitForTimeout(35)
    }
    expect(samples, 'the sampler ran no samples').toBeGreaterThan(3)
    expect(shotEarly).toBe(true)
    expect(shotDisplay).toBe(true)

    // Settled: the cascade is gone and the render of the same source holds — prices are
    // prose, the digit-led mathematics is mathematics, the unfinished equation is literal.
    const settled = await sample(page)
    expect(settled.katexDisplay, 'the display block was lost on settling').toBe(1)
    expect(
      settled.katexInline,
      'the digit-led spans $5x$ and $2+2$ must render as mathematics',
    ).toBe(2)
    expect(settled.text).toContain('It costs $5 and $10 today.')
    expect(settled.text).toContain('The limit is $\\frac{1}{')
    expect(settled.text).not.toContain('$$')
    expect(settled.fadedAncestors).toBe(0)

    const geometry = await page.evaluate(() => {
      const rect = (el: Element) => {
        const r = el.getBoundingClientRect()
        return {
          x: Math.round(r.x),
          y: Math.round(r.y),
          w: Math.round(r.width),
          h: Math.round(r.height),
        }
      }
      const root = Array.from(document.querySelectorAll('.assistant-content')).at(-1)!
      const display = root.querySelector('.katex-display')
      const li = display?.closest('li') ?? null
      return {
        display: display ? rect(display) : null,
        listItem: li ? rect(li) : null,
      }
    })
    writeFileSync(
      path.join(testInfo.outputDir, 'settled-geometry.json'),
      JSON.stringify({ ...geometry, settledText: settled.text }, null, 2),
    )
    await shot('settled.png')
  } finally {
    server.close()
  }
})
