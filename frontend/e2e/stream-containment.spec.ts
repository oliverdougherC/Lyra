/**
 * Renderer containment and ordered reveal (PLA-499 / PLA-500).
 *
 * The twin answer carries a lifted display equation in every container it can sit in — a
 * list item, a nested item, a blockquote list, an ordered list — plus the visuals the
 * cascade treats as single units: a code block, a table, a rule, and checkboxes.
 *
 * One test holds the static render: the equation stays inside the block it was written in,
 * the geometry of the blocks agrees with their markers, and nothing of the reveal cascade
 * is left on screen. The other streams the same answer over the network and checks the
 * cascade while it runs — the units reveal in reading order, the backlog never outruns the
 * stream, the code block arrives as one unit — and then checks that the turn settles onto
 * the static twin.
 */
import { expect, test } from '@playwright/test'

import {
  CLASS_ID,
  STREAM_TWIN,
  TWIN_MESSAGE_ID,
  TWIN_SESSION_ID,
  installLyraApi,
} from './pla-504-math-fixture'

const twinUrl = `/#/classes/${CLASS_ID}/chat?session=${TWIN_SESSION_ID}`

/**
 * The SSE body for a streamed agent turn: the answer in `token` frames, then the terminal
 * `result` frame whose `content` is the whole answer (EOF alone fails the turn, and a
 * `result` without `content`/`stopped`/`activity` is malformed). The frames land in one
 * body, but the reveal cascade is scheduled by deadline, not by arrival — so the answer's
 * words still reveal in order over the pacing interval, and the in-flight invariants are
 * observable after the stream itself has finished.
 */
function sseStream(text: string, messageId: number): Buffer {
  const frames: string[] = []
  for (let i = 0; i < text.length; i += 12) {
    frames.push(`data: ${JSON.stringify({ type: 'token', text: text.slice(i, i + 12) })}\n\n`)
  }
  frames.push(
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
  return Buffer.from(frames.join(''))
}

test.describe('renderer containment and ordered reveal', () => {
  test('settled: lifted display math stays inside its list, quote, and table', async ({ page }) => {
    await installLyraApi(page)
    await page.goto(twinUrl)
    const twin = page.locator('.assistant-content').last()
    await expect(twin.locator('.katex-display').first()).toBeVisible()

    const probe = await page.evaluate(() => {
      const root = document.querySelector('.assistant-content')
      if (!root) return null
      const displays = Array.from(root.querySelectorAll('.katex-display'))
      const rect = (el: Element) => el.getBoundingClientRect()
      const contains = (outer: Element, inner: Element) => {
        const a = rect(outer)
        const b = rect(inner)
        return b.left >= a.left - 8 && b.right <= a.right + 8
      }
      const displayReport = displays.map((display) => {
        const li = display.closest('li')
        const quote = display.closest('blockquote')
        const holder = li ?? quote ?? null
        return {
          text: display.textContent ?? '',
          inList: Boolean(li),
          inQuote: Boolean(quote),
          geometry: holder ? contains(holder, display) : false,
        }
      })
      const lists = {
        ul: root.querySelectorAll('ul').length,
        ol: root.querySelectorAll('ol').length,
        checkbox: root.querySelectorAll('input[type="checkbox"]').length,
        checked: root.querySelectorAll('input[type="checkbox"]:checked').length,
        nested: Array.from(root.querySelectorAll('li ul')).length,
      }
      const table = root.querySelector('table')
      const firstLi = root.querySelector('li')
      return {
        displayCount: displays.length,
        displays: displayReport,
        lists,
        tableRows: table ? table.querySelectorAll('tr').length : 0,
        tableMath: table ? table.querySelectorAll('.katex').length : 0,
        codeBlocks: root.querySelectorAll('pre').length,
        codeText: root.querySelector('pre code')?.textContent ?? null,
        rules: root.querySelectorAll('hr').length,
        leftoverUnits: root.querySelectorAll('[data-stream-word]').length,
        markerColor: firstLi ? getComputedStyle(firstLi, '::marker').color : null,
        textColor: firstLi ? getComputedStyle(firstLi).color : null,
        text: root.textContent ?? '',
      }
    })
    expect(probe, 'the twin message did not render').not.toBeNull()
    const p = probe!

    // Two lifted displays: one in a plain list item, one in a blockquote list.
    expect(p.displayCount).toBe(2)
    const listItemEquation = p.displays.find((d) => d.inList && !d.inQuote)
    const quotedEquation = p.displays.find((d) => d.inList && d.inQuote)
    expect(listItemEquation, 'no display equation inside a plain list item').toBeTruthy()
    expect(quotedEquation, 'no display equation inside the blockquote list').toBeTruthy()
    for (const display of p.displays) {
      expect(display.geometry, `display ${display.text} escaped its block`).toBe(true)
    }

    // The list groups stay single: one plain list, a nested list inside its item, the
    // blockquote's own list, and one ordered list.
    expect(p.lists.ol).toBe(1)
    expect(p.lists.nested).toBe(1)
    expect(p.lists.checkbox).toBe(2)
    expect(p.lists.checked).toBe(1)

    // The code block is whole, the table is a table, the rule is one.
    expect(p.codeBlocks).toBe(1)
    expect(p.codeText).toContain('const rate = 1 / (s + 2)')
    // A GFM table renders its header as one row and its single data row as one: the
    // separator line is structure, not a row.
    expect(p.tableRows).toBe(2)
    expect(p.tableMath).toBe(2)
    expect(p.rules).toBe(1)

    // A settled render carries no cascade: no units, and the list marker is drawn, not
    // left transparent by an animation that never ran.
    expect(p.leftoverUnits).toBe(0)
    expect(p.markerColor).not.toBe('rgba(0, 0, 0, 0)')
    expect(p.markerColor).toBe(p.textColor)

    // The source's markup is not in the rendered text.
    expect(p.text).toContain('Here is the full shape set.')
    expect(p.text).toContain('Done.')
    expect(p.text).not.toContain('$$')
    expect(p.text).not.toContain('```')
    expect(p.text).not.toContain('| - |')
  })

  test('streamed: ordered reveal with a bounded backlog, settling onto the static twin', async ({
    page,
  }) => {
    await installLyraApi(page)
    // The class chat page speaks to the contextual agent, so the turn rides its endpoint,
    // not the tutor's: register after installLyraApi so it wins over the catch-all.
    await page.route(
      `**/api/classes/${CLASS_ID}/sessions/${TWIN_SESSION_ID}/agent-chat`,
      (route) =>
        void route.fulfill({
          contentType: 'text/event-stream',
          body: sseStream(STREAM_TWIN, TWIN_MESSAGE_ID),
        }),
    )
    await page.goto(twinUrl)
    const staticTwin = page.locator('.assistant-content').last()
    await expect(staticTwin.locator('.katex-display').first()).toBeVisible()
    const staticText = (await staticTwin.textContent())?.trim() ?? ''
    expect(staticText).toContain('Here is the full shape set.')

    // Send a turn; the stream answers with the twin's content.
    await page.getByLabel('Message Lyra').fill('Stream the shape set.')
    await page.getByRole('button', { name: 'Send message' }).click()

    // The cascade is live: units exist and carry scheduled moments.
    await page.waitForFunction(() => {
      const units = Array.from(
        document.querySelectorAll<HTMLElement>('.assistant-content [data-stream-word]'),
      )
      return units.some((unit) => unit.dataset.streamRevealAt !== undefined)
    })

    // While the stream is still arriving, the invariants hold on every snapshot: the
    // reading-order deadlines are nondecreasing, and nothing is queued further than the
    // stream itself is allowed to lead.
    const snapshots: { times: number[]; lead: number }[] = []
    for (let i = 0; i < 4; i += 1) {
      const snapshot = await page.evaluate(() => {
        const units = Array.from(
          document.querySelectorAll<HTMLElement>('.assistant-content [data-stream-word]'),
        )
          .filter((unit) => unit.dataset.streamRevealAt !== undefined)
          .map((unit) => Number(unit.dataset.streamRevealAt))
        if (units.length === 0) return null
        return {
          times: units,
          // The lead over now: the queue may run ahead of the clock by at most the backlog cap.
          lead: Math.max(...units) - performance.now(),
        }
      })
      if (snapshot) snapshots.push(snapshot)
      await page.waitForTimeout(120)
    }
    expect(snapshots.length, 'no live cascade was observed').toBeGreaterThan(0)
    for (const { times, lead } of snapshots) {
      for (let i = 1; i < times.length; i += 1) {
        expect(
          times[i],
          `unit ${i} reveals before unit ${i - 1} (${times[i]} < ${times[i - 1]})`,
        ).toBeGreaterThanOrEqual(times[i - 1])
      }
      expect(lead, 'the reveal backlog outran the stream').toBeLessThanOrEqual(600)
    }

    // The code block is one unit, with no units inside it.
    const codeUnits = await page.evaluate(() => {
      const units = Array.from(
        document.querySelectorAll<HTMLElement>('.assistant-content [data-stream-word]'),
      )
      const code = units.filter((unit) => unit.dataset.streamWord?.startsWith('code-'))
      return {
        count: code.length,
        nested: code.some((unit) => unit.querySelector('[data-stream-word]')),
      }
    })
    expect(codeUnits.count).toBe(1)
    expect(codeUnits.nested).toBe(false)

    // The turn settles: the cascade is gone and the message the page now holds is the
    // static twin's text — the stream's final answer and the settled render agree.
    await page.waitForFunction(
      () => document.querySelectorAll('.assistant-content [data-stream-word]').length === 0,
      undefined,
      { timeout: 15000 },
    )
    const settledText =
      (await page.locator('.assistant-content').last().textContent())?.trim() ?? ''
    expect(settledText).toBe(staticText)
  })
})
