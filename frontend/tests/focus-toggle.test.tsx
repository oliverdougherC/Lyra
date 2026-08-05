import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { FocusToggle } from '@/components/solutions/focus-toggle'
import { TooltipProvider } from '@/components/ui/tooltip'

/**
 * Contract from docs/ui-phase-2.md: either pane can take the window. The split is the
 * right default, but half of what is left after the rail renders a Letter page at about
 * 47 DPI, and a 13-inch laptop is what this is built for.
 */
describe('FocusToggle', () => {
  it('offers the window and then offers it back', async () => {
    const onToggle = vi.fn()
    const { rerender } = render(
      <TooltipProvider>
        <FocusToggle focused={false} pane="the document" onToggle={onToggle} />
      </TooltipProvider>,
    )

    const expand = screen.getByRole('button', { name: 'Fill the window with the document' })
    expect(expand).toHaveAttribute('aria-pressed', 'false')
    await userEvent.click(expand)
    expect(onToggle).toHaveBeenCalledTimes(1)

    rerender(
      <TooltipProvider>
        <FocusToggle focused pane="the document" onToggle={onToggle} />
      </TooltipProvider>,
    )

    const collapse = screen.getByRole('button', { name: 'Back to both panes' })
    expect(collapse).toHaveAttribute('aria-pressed', 'true')
  })
})
