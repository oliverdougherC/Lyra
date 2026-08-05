'use client'

import { Maximize2, Minimize2 } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

type FocusToggleProps = {
  /** True when this pane already has the window to itself. */
  focused: boolean
  /** What this pane holds, for the label. */
  pane: string
  onToggle: () => void
}

/**
 * Give this pane the whole width, or hand the width back.
 *
 * The split is the right default: the point of the screen is a solution beside the sheet
 * it came from. But a 13-inch laptop is 1280px wide, and half of what is left after the
 * rail renders a Letter page at about 47 DPI. Reading the sheet closely, or reading a long
 * derivation without every line wrapping twice, is worth one pane for a minute.
 */
export function FocusToggle({ focused, pane, onToggle }: FocusToggleProps) {
  const label = focused ? 'Back to both panes' : `Fill the window with ${pane}`
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="text-text-tertiary hover:text-text-primary -mr-1 size-7 shrink-0 p-0 print:hidden"
          onClick={onToggle}
          aria-pressed={focused}
          aria-label={label}
        >
          {focused ? <Minimize2 className="size-3.5" /> : <Maximize2 className="size-3.5" />}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}
