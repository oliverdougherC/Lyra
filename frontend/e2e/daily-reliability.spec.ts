import { expect, test } from '@playwright/test'

import { CLASS_ID, SESSION_ID, installLyraApi } from './pla-504-math-fixture'

for (const viewport of [
  { name: 'narrow', width: 390, height: 560 },
  { name: 'desktop-minimum', width: 540, height: 600 },
  { name: 'desktop', width: 1200, height: 800 },
]) {
  for (const zoom of [1, 1.25]) {
    test(`chat composer remains reachable: ${viewport.name} at ${zoom}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(viewport)
      await page.emulateMedia({ reducedMotion: 'reduce' })
      await installLyraApi(page)
      await page.goto(`/#/classes/${CLASS_ID}/chat?session=${SESSION_ID}`)
      const input = page.getByRole('textbox', { name: 'Message Lyra' })
      await expect(input).toBeVisible()
      await page.evaluate(async (scale) => {
        document.documentElement.style.zoom = String(scale)
        await document.fonts.ready
        await new Promise<void>((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
        )
      }, zoom)
      await page.screenshot({ path: testInfo.outputPath('composer-layout.png') })
      const geometry = await page.evaluate(() => {
        const input = document.querySelector<HTMLTextAreaElement>('#message-composer')!
        const box = input.getBoundingClientRect()
        const nav = document.querySelector<HTMLElement>('[aria-label="Mobile navigation"]')!
        const navBox = nav.getBoundingClientRect()
        const shell = document
          .querySelector<HTMLElement>('[data-slot="sidebar-wrapper"]')!
          .getBoundingClientRect()
        return {
          composerTop: box.top,
          composerBottom: box.bottom,
          navTop: getComputedStyle(nav).display === 'none' ? innerHeight : navBox.top,
          shellBottom: shell.bottom,
          viewportHeight: innerHeight,
        }
      })
      expect(geometry.shellBottom).toBeLessThanOrEqual(geometry.viewportHeight + 1)
      expect(geometry.composerTop).toBeGreaterThan(0)
      expect(geometry.composerBottom).toBeLessThanOrEqual(geometry.navTop)
      await input.click()
      await input.fill('A synthetic question remains editable.')
      await expect(input).toBeFocused()
      const sidebar = page.locator('[data-slot="sidebar-container"]')
      if (await sidebar.isVisible()) {
        const settings = sidebar.getByRole('link', { name: 'Settings', exact: true })
        await expect(settings).toBeInViewport()
        await settings.click()
        await expect(page).toHaveURL(/#\/settings$/)
      }
      await page.emulateMedia({ media: 'print' })
      await expect(page.locator('[data-slot="sidebar-wrapper"]')).toHaveCSS('position', 'static')
    })
  }
}
