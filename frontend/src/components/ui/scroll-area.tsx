'use client'

import * as React from 'react'
import { ScrollArea as ScrollAreaPrimitive } from 'radix-ui'

import { cn } from '@/lib/utils'

function ScrollArea({
  className,
  children,
  viewportRef,
  scrollbar = true,
  ...props
}: React.ComponentProps<typeof ScrollAreaPrimitive.Root> & {
  /**
   * The scrolling element itself, for callers that drive or read scroll position.
   * Listen on it with `addEventListener`: the primitive sets its own `onScroll` on the
   * viewport, so a React `onScroll` prop passed down here would be overwritten.
   */
  viewportRef?: React.Ref<HTMLDivElement>
  /**
   * Draw the bar. Off leaves the area scrolling exactly as it did — the primitive hides
   * the platform scrollbar either way — with nothing drawn down the edge.
   */
  scrollbar?: boolean
}) {
  return (
    <ScrollAreaPrimitive.Root
      data-slot="scroll-area"
      className={cn('relative', className)}
      {...props}
    >
      {/* Radix sizes the viewport's content wrapper as `display: table`, which makes it
          shrink-to-fit and grow past the viewport whenever a child has intrinsic width
          (a wide table, a long code block). Forcing `block` keeps the column at the
          viewport width so prose wraps; wide children scroll in their own container. */}
      <ScrollAreaPrimitive.Viewport
        data-slot="scroll-area-viewport"
        ref={viewportRef}
        // The primitive makes the viewport scroll only where it has drawn a bar to scroll
        // it with — no bar, `overflow: hidden`, and a pane that a wheel cannot move. Said
        // here rather than left implied: the bar is a drawing, and whether one is drawn is
        // not the same question as whether the pane scrolls. The primitive hides the
        // platform's own scrollbar for every viewport it renders, so this shows nothing.
        style={scrollbar ? undefined : { overflowY: 'auto' }}
        className="size-full rounded-[inherit] transition-[color,box-shadow] outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-1 [&>div]:!block"
      >
        {children}
      </ScrollAreaPrimitive.Viewport>
      {scrollbar ? (
        <>
          <ScrollBar />
          <ScrollAreaPrimitive.Corner />
        </>
      ) : null}
    </ScrollAreaPrimitive.Root>
  )
}

function ScrollBar({
  className,
  orientation = 'vertical',
  ...props
}: React.ComponentProps<typeof ScrollAreaPrimitive.ScrollAreaScrollbar>) {
  return (
    <ScrollAreaPrimitive.ScrollAreaScrollbar
      data-slot="scroll-area-scrollbar"
      data-orientation={orientation}
      orientation={orientation}
      className={cn(
        'flex touch-none p-px transition-colors select-none data-horizontal:h-2.5 data-horizontal:flex-col data-horizontal:border-t data-horizontal:border-t-transparent data-vertical:h-full data-vertical:w-2.5 data-vertical:border-l data-vertical:border-l-transparent',
        className,
      )}
      {...props}
    >
      <ScrollAreaPrimitive.ScrollAreaThumb
        data-slot="scroll-area-thumb"
        className="relative flex-1 rounded-full bg-border"
      />
    </ScrollAreaPrimitive.ScrollAreaScrollbar>
  )
}

export { ScrollArea, ScrollBar }
