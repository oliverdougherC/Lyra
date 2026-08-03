'use client'

import * as React from 'react'
import { motion, useReducedMotion } from 'motion/react'

import { cn } from '@/lib/utils'

type RevealProps = React.ComponentProps<typeof motion.div> & {
  children: React.ReactNode
  className?: string
  delay?: number
}

const REVEAL_EASE: [number, number, number, number] = [0.25, 0.1, 0.3, 1]

export function Reveal({ children, className, delay = 0, ...props }: RevealProps) {
  const reduceMotion = useReducedMotion()
  const resolvedDelay = reduceMotion ? 0 : Math.min(Math.max(delay, 0), 0.2)

  return (
    <motion.div
      {...props}
      className={cn(className)}
      initial={reduceMotion ? { opacity: 0 } : { opacity: 0, y: 8 }}
      animate={reduceMotion ? { opacity: 1 } : { opacity: 1, y: 0 }}
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
