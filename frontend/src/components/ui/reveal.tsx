'use client'

import * as React from 'react'
import { motion, useReducedMotion } from 'motion/react'

import { cn } from '@/lib/utils'

type RevealProps = React.ComponentProps<typeof motion.div> & {
  children: React.ReactNode
  className?: string
  delay?: number
  /** Stable id: the reveal runs the first time this id is seen, and never again. */
  once?: string
}

const REVEAL_EASE: [number, number, number, number] = [0.25, 0.1, 0.3, 1]

/**
 * Reveals played once per session, keyed by `once`. Motion is meant to explain that
 * something arrived; replaying the cascade every time the user navigates back to a list
 * they have already seen explains nothing and reads as latency.
 */
const played = new Set<string>()

export function Reveal({ children, className, delay = 0, once, ...props }: RevealProps) {
  const reduceMotion = useReducedMotion()
  // Read during the first render so the decision is fixed before the element paints.
  const [skip] = React.useState(() => (once !== undefined ? played.has(once) : false))
  React.useEffect(() => {
    if (once !== undefined) played.add(once)
  }, [once])

  const resolvedDelay = reduceMotion || skip ? 0 : Math.min(Math.max(delay, 0), 0.2)

  return (
    <motion.div
      {...props}
      className={cn(className)}
      initial={skip ? false : reduceMotion ? { opacity: 0 } : { opacity: 0, y: 8 }}
      animate={reduceMotion || skip ? { opacity: 1 } : { opacity: 1, y: 0 }}
      transition={{
        duration: reduceMotion ? 0.15 : 0.25,
        ease: REVEAL_EASE,
        delay: resolvedDelay,
      }}
    >
      {children}
    </motion.div>
  )
}
