'use client'

import { ChevronRight } from 'lucide-react'

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { formatCount } from '@/lib/format'
import type { SolutionCheck } from '@/types'

type ToolCallTraceProps = {
  checks: SolutionCheck[]
}

/**
 * Every tool call the verifier made, closed by default.
 *
 * This is the audit trail, and it is the reason to believe the badge. A verdict the
 * student cannot inspect is a claim, not a check, so the count is always stated even
 * when nobody opens it.
 */
export function ToolCallTrace({ checks }: ToolCallTraceProps) {
  if (checks.length === 0) return null

  return (
    <Collapsible className="print:hidden">
      <CollapsibleTrigger className="text-text-tertiary hover:text-text-secondary focus-visible:ring-ring flex items-center gap-1 rounded-sm text-xs focus-visible:ring-2 focus-visible:outline-none [&[data-state=open]>svg]:rotate-90">
        <ChevronRight className="size-3 transition-transform duration-200" aria-hidden />
        {formatCount(checks.length, 'check')} run
      </CollapsibleTrigger>
      <CollapsibleContent>
        <ul className="border-border mt-2 flex flex-col gap-2 border-l pl-3">
          {checks.map((check, index) => (
            <li key={index} className="flex flex-col gap-0.5 text-xs">
              <span className="text-text-secondary font-mono">{check.tool}</span>
              <span className="text-text-tertiary font-mono break-all">{check.arguments}</span>
              <span className="text-text-tertiary font-mono break-all">
                {check.ok ? '' : 'failed: '}
                {check.result}
              </span>
            </li>
          ))}
        </ul>
      </CollapsibleContent>
    </Collapsible>
  )
}
