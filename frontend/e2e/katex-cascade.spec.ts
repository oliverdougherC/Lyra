/**
 * KaTeX cascade contract (PLA-504).
 *
 * Lyra's vendor math stylesheet has exactly one deliberate entry, and it sits in its own
 * `katex` cascade layer between `base` and `components`. That ordering is the whole reason
 * the chat can set inline mathematics in its own prose size while fractions and radicals
 * still keep the rules KaTeX draws for them:
 *
 *   - below `base` it loses to preflight's `*, ::before, ::after { border: 0 solid }`, and
 *     every fraction bar and rule line disappears;
 *   - unlayered, or above `components`, it beats Lyra's own overrides whatever their
 *     specificity, so inline chat maths inflates to KaTeX's 1.21em and a one-line preview
 *     becomes a block.
 *
 * These tests read computed styles out of a real browser against the production Vite build,
 * because none of that is visible to a source-string or jsdom assertion: jsdom resolves no
 * cascade layers at all, and the failure mode here is precisely a cascade-layer failure.
 */
import { resolve } from 'node:path'

import { expect, test, type Page } from '@playwright/test'

import {
  CLASS_ID,
  DRAFT_ID,
  SESSION_ID,
  SOLUTION_ID,
  installLyraApi,
  setTheme,
} from './pla-504-math-fixture'

/**
 * Screenshots go under the repository's `output/playwright`, as the repo convention has them.
 * `PLA504_SHOT_DIR` retargets the folder so the same suite can be run against a pre-fix build
 * and leave both sets of evidence behind.
 */
const shot = (name: string) =>
  resolve(
    __dirname,
    '..',
    '..',
    'output',
    'playwright',
    process.env.PLA504_SHOT_DIR ?? 'pla-504',
    name,
  )
/** Everything a cascade mistake moves. Read in one pass so the numbers are one frame. */
function probeMath(page: Page) {
  return page.evaluate(() => {
    const px = (value: string) => Number.parseFloat(value) || 0
    const box = (el: Element | null) => {
      if (!el) return null
      const rect = el.getBoundingClientRect()
      return { width: Number(rect.width.toFixed(2)), height: Number(rect.height.toFixed(2)) }
    }
    const read = (el: Element | null) => {
      if (!el) return null
      const s = getComputedStyle(el)
      return {
        fontSize: px(s.fontSize),
        fontFamily: s.fontFamily,
        color: s.color,
        display: s.display,
        marginTop: px(s.marginTop),
        marginBottom: px(s.marginBottom),
        marginLeft: px(s.marginLeft),
        marginRight: px(s.marginRight),
        borderTopWidth: px(s.borderTopWidth),
        borderBottomWidth: px(s.borderBottomWidth),
        borderBottomStyle: s.borderBottomStyle,
        borderTopStyle: s.borderTopStyle,
        overflowX: s.overflowX,
        textIndent: s.textIndent,
        box: box(el),
        scrollWidth: el.scrollWidth,
        clientWidth: el.clientWidth,
      }
    }
    const all = (selector: string) => Array.from(document.querySelectorAll(selector))
    const first = (selector: string) => all(selector)[0] ?? null

    const surface = first('.assistant-content')
    const paragraphWithMath = all('.assistant-content p').find((p) =>
      p.querySelector(':scope > .katex'),
    )
    const inline = paragraphWithMath?.querySelector(':scope > .katex') ?? null
    const display = first('.assistant-content .katex-display')
    const displayMath = first('.assistant-content .katex-display > .katex')
    const fraction = first('.assistant-content .katex-display .frac-line')
    const overline = first('.assistant-content .overline-line')
    const radical = first('.assistant-content .katex-display .mord.sqrt svg')
    const radicalSign = first('.assistant-content .katex-display .mord.sqrt')
    const mathml = first('.assistant-content math')
    const annotation = first('.assistant-content annotation')

    return {
      root: {
        themeText: getComputedStyle(document.documentElement)
          .getPropertyValue('--text-primary')
          .trim(),
        katexFontsLoaded: document.fonts.check('16px KaTeX_Main'),
        loadedFontFaces: Array.from(document.fonts)
          .map((face) => face.family)
          .filter((family) => family.startsWith('KaTeX'))
          .filter((family, index, list) => list.indexOf(family) === index)
          .sort(),
      },
      surface: read(surface),
      prose: read(paragraphWithMath ?? null),
      inline: read(inline),
      display: read(display),
      displayMath: read(displayMath),
      fraction: read(fraction),
      overline: read(overline),
      radical: read(radicalSign),
      radicalBox: box(radical),
      mathml: {
        present: Boolean(mathml),
        annotation: annotation?.textContent?.trim() ?? null,
      },
      counts: {
        inline: all('.assistant-content p > .katex').length,
        display: all('.assistant-content .katex-display').length,
        fractions: all('.assistant-content .frac-line').length,
        radicals: all('.assistant-content .mord.sqrt').length,
      },
      page: {
        innerWidth: window.innerWidth,
        scrollWidth: document.documentElement.scrollWidth,
      },
    }
  })
}

type Probe = Awaited<ReturnType<typeof probeMath>>

/** The cascade layers the browser actually resolved, in the order it resolved them. */
function layerOrder(page: Page) {
  return page.evaluate(() => {
    const names: string[] = []
    for (const sheet of Array.from(document.styleSheets)) {
      let rules: CSSRuleList | null
      try {
        rules = sheet.cssRules
      } catch {
        continue
      }
      if (!rules) continue
      for (const rule of Array.from(rules)) {
        const name =
          (rule as CSSLayerBlockRule).name ?? (rule as CSSLayerStatementRule).nameList?.join(',')
        if (!name) continue
        for (const layer of name.split(',')) {
          const trimmed = layer.trim()
          if (trimmed && !names.includes(trimmed)) names.push(trimmed)
        }
      }
    }
    return names
  })
}

/** The vendor rules that must not be visible unlayered in the document's sheets. */
function unlayeredKatexRules(page: Page) {
  return page.evaluate(() => {
    const hits: { sheet: string; selector: string }[] = []
    const walk = (rules: CSSRuleList, sheet: string, layered: boolean) => {
      for (const rule of Array.from(rules)) {
        if (rule instanceof CSSLayerBlockRule || rule instanceof CSSLayerStatementRule) continue
        if (rule instanceof CSSGroupingRule) {
          walk(rule.cssRules, sheet, layered)
          continue
        }
        if (rule instanceof CSSStyleRule && !layered) {
          const selector = rule.selectorText
          // A vendor rule: it hangs off a KaTeX class of its own. Lyra's own overrides are
          // always anchored on a Lyra class or an attribute of ours.
          // A vendor rule hangs off a KaTeX class of its own; Lyra's overrides are always
          // anchored on a Lyra class or on an attribute of ours.
          const vendor = selector
            .split(',')
            .some(
              (part) =>
                /^\.katex/.test(part.trim()) &&
                !/math-text|assistant-content|draft-editor|reasoning-body|stream-word|\[/.test(
                  part,
                ),
            )
          if (vendor) hits.push({ sheet, selector })
        }
      }
    }
    for (const sheet of Array.from(document.styleSheets)) {
      let rules: CSSRuleList | null
      try {
        rules = sheet.cssRules
      } catch {
        continue
      }
      if (!rules) continue
      walk(rules, sheet.href ?? 'inline', false)
    }
    return hits
  })
}

/** Compare two probes for the properties a restyle would move. */
function cascadeSignature(probe: Probe) {
  const pick = (el: Probe['inline']) =>
    el
      ? {
          fontSize: el.fontSize,
          display: el.display,
          marginTop: el.marginTop,
          marginBottom: el.marginBottom,
          fontFamily: el.fontFamily,
          borderBottomWidth: el.borderBottomWidth,
          borderTopWidth: el.borderTopWidth,
        }
      : null
  return {
    inline: pick(probe.inline),
    display: pick(probe.display),
    displayMath: pick(probe.displayMath),
    fraction: pick(probe.fraction),
    overline: pick(probe.overline),
    radicalBox: probe.radicalBox,
  }
}

/**
 * Painted-ink measurement. A computed `border-bottom-width` is a promise; the pixel run is
 * the bar. The element is screenshotted and sampled back in the page, so this reads what the
 * browser drew rather than what a declaration says it should have drawn.
 */
async function inkRows(page: Page, target: ReturnType<Page['locator']>) {
  const png = await target.screenshot({ scale: 'css' })
  const dataUrl = `data:image/png;base64,${png.toString('base64')}`
  return page.evaluate(async (url) => {
    const image = new Image()
    image.src = url
    await image.decode()
    const canvas = document.createElement('canvas')
    canvas.width = image.width
    canvas.height = image.height
    const context = canvas.getContext('2d')!
    context.drawImage(image, 0, 0)
    const { data } = context.getImageData(0, 0, canvas.width, canvas.height)
    const at = (x: number, y: number) => {
      const i = (y * canvas.width + x) * 4
      return [data[i], data[i + 1], data[i + 2], data[i + 3]] as const
    }
    // The corner is the sheet the maths is set on; anything a long way from it is ink.
    const background = at(0, 0)
    const rows: number[] = []
    for (let y = 0; y < canvas.height; y++) {
      let ink = 0
      for (let x = 0; x < canvas.width; x++) {
        const [r, g, b, a] = at(x, y)
        if (a < 128) continue
        const distance =
          Math.abs(r - background[0]) + Math.abs(g - background[1]) + Math.abs(b - background[2])
        if (distance > 90) ink++
      }
      rows.push(canvas.width ? Number((ink / canvas.width).toFixed(4)) : 0)
    }
    return { width: canvas.width, height: canvas.height, rows }
  }, dataUrl)
}

const bandPeak = (rows: number[], from: number, to: number) =>
  rows
    .slice(
      Math.floor(rows.length * from),
      Math.max(Math.floor(rows.length * from) + 1, Math.ceil(rows.length * to)),
    )
    .reduce((peak, row) => Math.max(peak, row), 0)

test.describe('KaTeX cascade in the chat surface', () => {
  for (const theme of ['light', 'dark'] as const) {
    test(`inline maths is set in the prose size, and display maths in its own size (${theme})`, async ({
      page,
    }) => {
      await setTheme(page, theme)
      await installLyraApi(page)
      await page.goto(`/#/classes/${CLASS_ID}/chat?session=${SESSION_ID}`)
      await expect(page.locator('.assistant-content .katex-display').first()).toBeVisible()

      // Evidence first: a failing assertion below must not cost us the screenshot.
      await page.screenshot({ path: shot(`chat-${theme}.png`) })

      const probe = await probeMath(page)
      expect(probe.counts.inline, 'the fixture rendered no inline maths').toBeGreaterThan(0)
      expect(probe.counts.display, 'the fixture rendered no display maths').toBeGreaterThan(0)
      expect(probe.inline, 'no inline .katex element').not.toBeNull()

      // Inline maths is prose: the same size as the paragraph it sits in, not KaTeX's 1.21em.
      const prose = probe.prose!.fontSize
      expect(probe.inline!.fontSize, `inline maths escaped the prose size in ${theme}`).toBe(prose)
      expect(probe.inline!.fontFamily).toContain('KaTeX_Main')

      // Display maths is deliberately a little larger than prose, and block-level.
      expect(probe.display!.display).toBe('block')
      expect(probe.displayMath!.display).toBe('block')
      expect(probe.displayMath!.fontSize).toBeGreaterThan(prose)
      expect(probe.displayMath!.fontSize).toBeCloseTo(prose * 1.12, 1)

      // The theme reaches the mathematics: it takes the ink of the surface it is set on.
      expect(probe.root.themeText).not.toBe('')
      expect(probe.inline!.color).toBe(probe.surface!.color)

      // Accessible MathML survived the stylesheet ordering, with its TeX annotation.
      expect(probe.mathml.present).toBe(true)
      expect(probe.mathml.annotation).toContain('e^{-2t}u(t-3)')

      // The vendor web fonts are the ones in use, from the single entry's own @font-face set.
      expect(probe.root.katexFontsLoaded).toBe(true)
      expect(probe.root.loadedFontFaces).toContain('KaTeX_Main')

      expect(
        await unlayeredKatexRules(page),
        'the vendor KaTeX sheet is reaching the document unlayered',
      ).toEqual([])
      const layers = await layerOrder(page)
      expect(layers.indexOf('base'), `resolved layer order: ${layers.join(' < ')}`).toBeLessThan(
        layers.indexOf('katex'),
      )
      expect(layers.indexOf('katex'), `resolved layer order: ${layers.join(' < ')}`).toBeLessThan(
        layers.indexOf('components'),
      )
    })

    test(`fractions, radicals and rule lines are drawn (${theme})`, async ({ page }) => {
      await setTheme(page, theme)
      await installLyraApi(page)
      await page.goto(`/#/classes/${CLASS_ID}/chat?session=${SESSION_ID}`)
      await expect(page.locator('.assistant-content .katex-display').first()).toBeVisible()

      await page
        .locator('.assistant-content')
        .first()
        .screenshot({ path: shot(`chat-maths-${theme}.png`) })

      const probe = await probeMath(page)
      expect(probe.counts.fractions, 'the fixture rendered no fraction').toBeGreaterThan(0)
      expect(probe.counts.radicals, 'the fixture rendered no radical').toBeGreaterThan(0)

      // Preflight's `*, ::before, ::after { border: 0 solid }` outranks these by layer order
      // alone, which is how every rule line in the app used to disappear.
      expect(probe.fraction, 'no .frac-line element').not.toBeNull()
      expect(probe.fraction!.borderBottomWidth).toBeGreaterThan(0)
      expect(probe.fraction!.borderBottomStyle).toBe('solid')
      expect(probe.overline, 'no .overline-line element').not.toBeNull()
      expect(
        Math.max(probe.overline!.borderTopWidth, probe.overline!.borderBottomWidth),
      ).toBeGreaterThan(0)

      // The radical's bar is a filled path in the sqrt sign's own svg box.
      expect(probe.radical, 'no radical svg').not.toBeNull()
      expect(probe.radical!.box!.height).toBeGreaterThan(6)
      expect(probe.radicalBox!.width).toBeGreaterThan(4)

      // And the ink is actually there: a fraction bar is a full-width horizontal run through
      // the middle of the fraction, and a radical bar a run across the top of the radical.
      const fraction = await inkRows(
        page,
        page.locator('.assistant-content .katex-display .mfrac').first(),
      )
      const overline = await inkRows(page, page.locator('.assistant-content .mord.sqrt').first())
      const evidence = {
        theme,
        fraction: {
          width: fraction.width,
          height: fraction.height,
          middlePeak: bandPeak(fraction.rows, 0.42, 0.58),
        },
        radical: {
          width: overline.width,
          height: overline.height,
          topPeak: bandPeak(overline.rows, 0, 0.3),
        },
      }
      console.log(`PAINTED INK (${theme}):`, JSON.stringify(evidence))
      expect(
        evidence.fraction.middlePeak,
        `no painted fraction bar: ${JSON.stringify(evidence.fraction)}`,
      ).toBeGreaterThan(0.5)
      expect(
        evidence.radical.topPeak,
        `no painted radical bar across the top: ${JSON.stringify(evidence.radical)}`,
      ).toBeGreaterThan(0.2)
    })

    test(`a long equation overflows its own box, not the page (${theme})`, async ({ page }) => {
      await setTheme(page, theme)
      await installLyraApi(page)
      await page.setViewportSize({ width: 760, height: 900 })
      await page.goto(`/#/classes/${CLASS_ID}/chat?session=${SESSION_ID}`)
      await expect(page.locator('.assistant-content .katex-display').last()).toBeVisible()

      await page.screenshot({ path: shot(`chat-overflow-${theme}.png`) })

      const wide = await page.evaluate(() => {
        const surfaces = Array.from(
          document.querySelectorAll('.assistant-content .katex-display'),
        ).map((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }))
        return {
          surfaces,
          pageScrollWidth: document.documentElement.scrollWidth,
          innerWidth: window.innerWidth,
        }
      })
      const overflowing = wide.surfaces.filter((s) => s.scrollWidth > s.clientWidth)
      expect(overflowing.length, 'no display equation was wide enough to overflow').toBeGreaterThan(
        0,
      )
      expect(
        wide.pageScrollWidth,
        'a long equation widened the document instead of its own box',
      ).toBeLessThanOrEqual(wide.innerWidth)
    })
  }

  test('the solver review row typesets maths without leaving its row', async ({ page }) => {
    await setTheme(page, 'light')
    await installLyraApi(page)
    await page.goto(`/#/classes/${CLASS_ID}/solutions/${SOLUTION_ID}`)
    await expect(page.locator('.math-text').first()).toBeVisible()

    await page
      .locator('.math-text')
      .first()
      .screenshot({ path: shot('solver-row-light.png') })

    const probe = await page.evaluate(() => {
      const px = (v: string) => Number.parseFloat(v) || 0
      const rows = Array.from(document.querySelectorAll('.math-text'))
      const styled = (el: Element) => {
        const s = getComputedStyle(el)
        return {
          fontSize: px(s.fontSize),
          display: s.display,
          marginTop: px(s.marginTop),
          marginBottom: px(s.marginBottom),
          overflowX: s.overflowX,
        }
      }
      const withMath = rows.find((el) => el.querySelector('.katex')) ?? null
      const withDisplay = rows.find((el) => el.querySelector('.katex-display')) ?? null
      return {
        rowCount: rows.length,
        inlineRow: withMath ? styled(withMath) : null,
        inlineMath: withMath ? styled(withMath.querySelector('.katex')!) : null,
        displayRow: withDisplay ? styled(withDisplay) : null,
        displayMath: withDisplay ? styled(withDisplay.querySelector('.katex-display')!) : null,
        displayMathInner: withDisplay
          ? styled(withDisplay.querySelector('.katex-display > .katex')!)
          : null,
        pageScrollWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth,
      }
    })
    expect(probe.inlineMath, 'no review row typeset any maths').not.toBeNull()
    // A statement row sets its own size; maths rides it at 1.05em rather than growing past it
    // the way the vendor sheet's 1.21em does.
    expect(probe.inlineMath!.fontSize).toBeCloseTo(probe.inlineRow!.fontSize * 1.05, 1)
    expect(probe.pageScrollWidth).toBeLessThanOrEqual(probe.innerWidth)

    expect(probe.displayMath, 'no review row held a display equation').not.toBeNull()
    expect(probe.displayMath!.display).toBe('block')
    // The row's own rhythm, not the chat's: a half-em of space, not a 1.25rem block.
    expect(probe.displayMath!.marginTop).toBeGreaterThan(0)
    expect(probe.displayMath!.marginTop).toBeLessThanOrEqual(
      Math.round(probe.displayRow!.fontSize * 0.5),
    )
    expect(probe.displayMathInner!.display).toBe('block')
  })

  // Lazy editor navigation must preserve the same cascade in both themes.
  for (const theme of ['light', 'dark'] as const) {
    test.describe(`lazy editor cascade (${theme})`, () => {
      test('an inline preview holds a display equation on one line', async ({ page }) => {
        await setTheme(page, theme)
        await installLyraApi(page)
        await page.goto(`/#/classes/${CLASS_ID}/drafts/${DRAFT_ID}`)
        await expect(page.locator('.ProseMirror').first()).toBeVisible()
        // The margin panel, where a review comment quotes a passage of the draft.
        await page.getByRole('combobox').first().click()
        await page
          .getByRole('option', { name: /Comments/ })
          .first()
          .click()
        const preview = page.locator('.math-text-inline .katex-display').first()
        await expect(preview).toBeVisible()
        await page
          .locator('.math-text-inline')
          .first()
          .screenshot({ path: shot(`inline-preview-${theme}.png`) })

        const probe = await page.evaluate(() => {
          const px = (v: string) => Number.parseFloat(v) || 0
          const preview = document.querySelector('.math-text-inline .katex-display')!
          const inner = document.querySelector('.math-text-inline .katex-display > .katex')
          const container = preview.closest('.math-text-inline')!
          const style = (el: Element | null) => {
            if (!el) return null
            const s = getComputedStyle(el)
            return {
              display: s.display,
              marginTop: px(s.marginTop),
              marginBottom: px(s.marginBottom),
              marginLeft: px(s.marginLeft),
              marginRight: px(s.marginRight),
              fontSize: px(s.fontSize),
            }
          }
          return {
            preview: style(preview),
            inner: style(inner),
            containerHeight: Number(container.getBoundingClientRect().height.toFixed(2)),
            lineHeight: getComputedStyle(container).lineHeight,
          }
        })
        expect(probe.preview!.display, 'an inline preview took a block display').toBe('inline')
        expect(
          probe.inner!.display,
          'the maths inside an inline preview took a block display',
        ).toBe('inline')
        expect(probe.preview!.marginTop).toBe(0)
        expect(probe.preview!.marginBottom).toBe(0)
        // One line means one line: the quoted passage never grew into its own block.
        expect(probe.containerHeight).toBeLessThan(40)
      })

      test('entering and leaving the draft editor leaves chat maths alone', async ({ page }) => {
        await setTheme(page, theme)
        await installLyraApi(page)
        await page.goto(`/#/classes/${CLASS_ID}/chat?session=${SESSION_ID}`)
        await expect(page.locator('.assistant-content .katex-display').first()).toBeVisible()
        const before = cascadeSignature(await probeMath(page))
        await page.screenshot({ path: shot(`chat-before-draft-${theme}.png`) })

        // Client-side navigation: the draft workspace is a lazy chunk with its own stylesheet,
        // which is a second chance for vendor maths CSS to arrive unlayered.
        await page.evaluate((href) => {
          window.location.hash = href
        }, `/classes/${CLASS_ID}/drafts/${DRAFT_ID}`)
        await expect(page.locator('.ProseMirror').first()).toBeVisible()
        await expect(page.locator('.assistant-content .katex-display').first()).toBeVisible()
        const onDraft = cascadeSignature(await probeMath(page))
        const unlayered = await unlayeredKatexRules(page)
        await page.screenshot({ path: shot(`chat-on-draft-${theme}.png`) })

        await page.evaluate((href) => {
          window.location.hash = href
        }, `/classes/${CLASS_ID}/chat?session=${SESSION_ID}`)
        await expect(page.locator('.assistant-content .katex-display').first()).toBeVisible()
        const after = cascadeSignature(await probeMath(page))
        await page.screenshot({ path: shot(`chat-after-draft-${theme}.png`) })

        console.log(
          'DRAFT ENTRY/EXIT:',
          JSON.stringify({
            sheets: Array.from(new Set(unlayered.map((hit) => hit.sheet))),
            before,
            onDraft,
            after,
          }),
        )
        expect(
          Array.from(new Set(unlayered.map((hit) => hit.sheet))),
          'a second, unlayered KaTeX sheet reached the document with the editor chunk',
        ).toEqual([])
        expect(onDraft, 'opening the draft editor restyled the chat maths beside it').toEqual(
          before,
        )
        expect(after, 'leaving the draft editor restyled the chat maths').toEqual(before)
      })
    })
  }
})
