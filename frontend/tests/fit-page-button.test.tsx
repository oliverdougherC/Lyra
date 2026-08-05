import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { FitPageButton } from '@/components/solutions/fit-page-button'
import { TooltipProvider } from '@/components/ui/tooltip'

function renderButton(fitted: boolean, onFit = vi.fn()) {
  render(
    <TooltipProvider>
      <FitPageButton fitted={fitted} onFit={onFit} />
    </TooltipProvider>,
  )
  return onFit
}

/**
 * The width that shows a whole page at its largest is measured, not guessed — but dragging
 * the split overrides it for good, and before this there was no way back to it short of
 * nudging the divider until the page looked right.
 */
describe('FitPageButton', () => {
  it('asks for the fit when the column has been dragged off it', async () => {
    const onFit = renderButton(false)

    await userEvent.click(screen.getByRole('button', { name: 'Fit the page to the pane' }))

    expect(onFit).toHaveBeenCalledTimes(1)
  })

  it('cannot be pressed when the page is already at that size', async () => {
    // Disabled rather than absent. The condition flips every time the divider moves, and a
    // control that came and went as you dragged would be harder to find than an unlit one.
    const onFit = renderButton(true)
    const button = screen.getByRole('button', { name: 'The page is already at its best size' })

    expect(button).toBeDisabled()
    await userEvent.click(button)
    expect(onFit).not.toHaveBeenCalled()
  })

  it('says which of the two states it is in, in the label rather than only in colour', () => {
    const { unmount } = render(
      <TooltipProvider>
        <FitPageButton fitted={false} onFit={vi.fn()} />
      </TooltipProvider>,
    )
    expect(screen.getByRole('button', { name: /Fit the page/ })).toBeInTheDocument()
    unmount()

    renderButton(true)
    expect(screen.getByRole('button', { name: /already at its best size/ })).toBeInTheDocument()
  })
})
