'use client'

import type { SolutionCheck } from '@/types'

type ToolCallTraceProps = {
  checks: SolutionCheck[]
}

/**
 * Every tool call the verifier made, in the order it ran.
 *
 * The audit trail behind a verdict: raw names, arguments, and results. It is the reason to
 * believe the badge, and it lives entirely inside the "How Lyra checked this" disclosure -
 * the worked answer is the document's primary content, and the machinery that checked it
 * is what you open when you want to see it. The count of checks travels with the
 * disclosure's trigger, so the audit is never presented as if it were the answer.
 */
export function ToolCallTrace({ checks }: ToolCallTraceProps) {
  if (checks.length === 0) return null

  return (
    <ul className="flex flex-col gap-2">
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
  )
}
