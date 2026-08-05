import * as React from 'react'

import { cn } from '@/lib/utils'

function Textarea({ className, ...props }: React.ComponentProps<'textarea'>) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        // No ring offset. A caller that turns the ring off with `ring-0` does not turn the
        // offset off with it, and the offset paints a 2px rectangle in the page background
        // colour regardless: inside the composer, on a dark theme, that reads as a black
        // box sitting in the input. The ring is legible flush against the field anyway.
        'flex field-sizing-content min-h-16 w-full rounded-md border border-input bg-card px-3 py-2 text-base transition-colors outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:bg-muted disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-2 aria-invalid:ring-destructive/30 md:text-sm',
        className,
      )}
      {...props}
    />
  )
}

export { Textarea }
