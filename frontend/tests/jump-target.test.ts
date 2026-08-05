import { describe, expect, it, vi } from 'vitest'

import { jumpTargetTop } from '@/components/solutions/solution-workspace'

/**
 * The problem headings in the solutions pane are `sticky top-0` inside their own problem,
 * so a heading that has scrolled out of view is pinned to the bottom edge of its container
 * rather than left behind above the pane. Measuring one therefore reports the end of that
 * problem, which is the start of the next: jumping back from problem four to problem one
 * landed on the top of problem two, while every forward jump looked fine.
 *
 * These build the lie deliberately. Each item exposes an honest static box and a heading
 * whose rect is pinned to the item's bottom, which is what the browser really returns, and
 * the assertions are that the answer comes out right anyway.
 */
const GAP = 8
const PADDING = 32

function element(rect: { top: number; bottom: number }, paddingTop = '0px'): HTMLElement {
  return {
    getBoundingClientRect: () => ({ top: rect.top, bottom: rect.bottom }),
    style: { paddingTop },
  } as unknown as HTMLElement
}

/** A problem whose heading is stuck to the bottom of its own box, as a scrolled-past one is. */
function stuckItem(top: number, height: number, paddingTop = `${PADDING}px`) {
  return element({ top, bottom: top + height }, paddingTop)
}

function viewport(scrollTop: number, top = 0): HTMLElement {
  return {
    getBoundingClientRect: () => ({ top, bottom: top + 600 }),
    scrollTop,
  } as unknown as HTMLElement
}

function withComputedStyle<T>(run: () => T): T {
  const spy = vi
    .spyOn(window, 'getComputedStyle')
    .mockImplementation((node) => ({ paddingTop: (node as HTMLElement).style.paddingTop }) as never)
  try {
    return run()
  } finally {
    spy.mockRestore()
  }
}

describe('jumpTargetTop', () => {
  it('lands a backward jump on the problem asked for, not the one after it', () => {
    // Problem one is 2758px tall and sits far above the reader, exactly as it does when
    // they are on problem four. Its heading's own rect would say -3964, the bottom of the
    // box; the heading actually belongs at the top of it.
    const item = stuckItem(-6673, 2758, '0px')

    const top = withComputedStyle(() => jumpTargetTop(viewport(6800), item, GAP))

    expect(top).toBe(6800 - 6673 - GAP)
  })

  it('leaves the gap above a problem it has to travel forward to', () => {
    const item = stuckItem(103.9, 2000)

    const top = withComputedStyle(() => jumpTargetTop(viewport(500), item, GAP))

    expect(top).toBe(500 + 103.9 + PADDING - GAP)
  })

  it('clears the padding that separates a problem from the one above it', () => {
    // Aiming at the item's own box left the title 32px down an otherwise empty pane, which
    // is the reason this ever measured the heading in the first place.
    const spaced = stuckItem(1000, 500)
    const first = stuckItem(1000, 500, '0px')

    const [withGap, withoutGap] = withComputedStyle(() => [
      jumpTargetTop(viewport(0), spaced, GAP),
      jumpTargetTop(viewport(0), first, GAP),
    ])

    expect(withGap - withoutGap).toBe(PADDING)
  })

  it('never asks the viewport to scroll above its own top', () => {
    // The first problem sits within `JUMP_GAP_PX` of the top, so the gap would take the
    // target negative and the pane would refuse the trip.
    const item = stuckItem(0, 500, '0px')

    const top = withComputedStyle(() => jumpTargetTop(viewport(0), item, GAP))

    expect(top).toBe(0)
  })

  it('measures from the pane rather than from the window', () => {
    // The solutions pane is not at the top of the screen; the header and the tab strip sit
    // above it. A target computed against the window would land every jump too low by
    // however tall those happen to be.
    const item = stuckItem(300, 500, '0px')

    const top = withComputedStyle(() => jumpTargetTop(viewport(0, 120), item, GAP))

    expect(top).toBe(300 - 120 - GAP)
  })
})
