'use client'

import { Scan } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

type FitPageButtonProps = {
  /** True when the column is already sized to the page, so there is nothing to do. */
  fitted: boolean
  onFit: () => void
}

/**
 * Put the document column back to the width that shows a whole page at its largest.
 *
 * That width is already known — the pane measures it and reports it as it decodes a page —
 * and it is what the column starts at. What it is not is sticky: dragging the split is a
 * choice, and a choice outranks a measurement for as long as it stands, so a reader who
 * dragged once had no way back to the size the page actually wants short of nudging the
 * divider until it looked right. This is that way back.
 *
 * Disabled rather than hidden while the fit is already in force. The condition flips every
 * time the divider moves, and a control that came and went as you dragged would be harder
 * to find than one that is simply not lit.
 */
export function FitPageButton({ fitted, onFit }: FitPageButtonProps) {
  const label = fitted ? 'The page is already at its best size' : 'Fit the page to the pane'
  const button = (
    <Button
      variant="ghost"
      size="sm"
      className="text-text-tertiary hover:text-text-primary size-7 shrink-0 p-0 print:hidden"
      onClick={onFit}
      disabled={fitted}
      aria-label={label}
    >
      <Scan className="size-3.5" />
    </Button>
  )
  // A disabled trigger receives no pointer events, so Radix would never open its tooltip.
  // Nothing is lost by leaving it off: the label says what the button is either way, and a
  // control that cannot be pressed has nothing to explain about pressing it.
  if (fitted) return button
  return (
    <Tooltip>
      <TooltipTrigger asChild>{button}</TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}
