'use client'

import * as React from 'react'

import { cn } from '@/lib/utils'

type RevealProps = React.ComponentProps<'div'> & {
  children: React.ReactNode
  className?: string
  /** Seconds, capped at 200ms so a long list never spends a second cascading in. */
  delay?: number
  /** Stable id: the reveal runs the first time this id is seen, and never again. */
  once?: string
}

/**
 * Reveals played once per session, keyed by `once`. Motion is meant to explain that
 * something arrived; replaying the cascade every time the user navigates back to a list
 * they have already seen explains nothing and reads as latency.
 */
const played = new Set<string>()

/**
 * A CSS animation rather than a JS-driven one, deliberately. The script-driven version
 * froze whenever the main thread or its rAF loop stalled during hydration, leaving rows
 * stranded at near-zero opacity — content lost to a decoration. A compositor animation
 * with `both` fill cannot strand anything: however the frames are scheduled, it ends at
 * fully visible, and the global reduced-motion rule truncates it to a single frame.
 */
export function Reveal({ children, className, delay = 0, once, ...props }: RevealProps) {
  // Read during the first render so the decision is fixed before the element paints.
  const [skip] = React.useState(() => (once !== undefined ? played.has(once) : false))
  React.useEffect(() => {
    if (once !== undefined) played.add(once)
  }, [once])

  const resolvedDelay = Math.min(Math.max(delay, 0), 0.2)

  return (
    <div
      {...props}
      className={cn(!skip && 'lyra-reveal', className)}
      style={!skip && resolvedDelay > 0 ? { animationDelay: `${resolvedDelay}s` } : undefined}
    >
      {children}
    </div>
  )
}
