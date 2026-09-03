import { useMediaQuery } from '@/lib/hooks/use-media-query'

/**
 * The one structural breakpoint of the shell: below this width the navigation rail is a
 * temporary sheet (opened from the header trigger or Ctrl-B) rather than a docked column.
 *
 * It is measured against task space, not device class: the rail takes 260px of the window
 * it is docked in, and at 1024px that is a quarter of the work surface the page still has
 * to keep. 768px is not the threshold merely because it is a familiar one - a 768px
 * window beside a code editor is Lyra's ordinary working size, and a docked rail there
 * consumed a third of it. The workbench columns use the same 1024px line, so the shell
 * and the workspaces decide their layout from one fact.
 */
const RAIL_BREAKPOINT = 1024

/** True while the rail is a temporary sheet rather than a docked column. */
export function useIsMobile() {
  return useMediaQuery(`(max-width: ${RAIL_BREAKPOINT - 1}px)`)
}
