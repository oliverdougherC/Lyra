'use client'

import { Button } from '@/components/ui/button'

/**
 * Client-route error fallback. When a lazy route throws while rendering, this
 * component replaces the failed route; the rail and header remain available so the
 * student is never cut off from navigation. `retry()` remounts the route.
 *
 * Privacy contract: the `error` prop is typed for the boundary contract only.
 * Its `message`, `digest`, stack, and any application state it carries never
 * reach the DOM. The copy below is fixed and says the same thing whatever
 * threw, so the fallback can neither leak a draft, a course document, an API
 * body, nor a path. Nothing here reports the error anywhere: Lyra keeps every
 * byte of failure local (docs/privacy-and-data-location.md).
 */
export default function Error({
  error,
  retry,
}: {
  error: Error & { digest?: string }
  retry: () => void
}) {
  // `error` is deliberately unused: reading it is how a fallback would leak.
  void error

  return (
    <div role="alert" className="flex flex-1 items-center justify-center p-4">
      <div className="w-full max-w-md rounded-lg border border-border-strong bg-card p-6 text-center shadow-md sm:p-8">
        <h2 className="font-heading text-base font-medium tracking-tight">Something went wrong</h2>
        <p className="mt-3 text-sm/relaxed text-muted-foreground">
          An unexpected error stopped Lyra from drawing this page. Try again, or go back and reopen
          it.
        </p>
        <Button className="mt-6" onClick={retry}>
          Try again
        </Button>
      </div>
    </div>
  )
}
