'use client'

import { forwardRef, useEffect, useState } from 'react'

type DynamicOptions = {
  loading?: () => React.ReactNode
  error?: (error: Error, retry: () => void) => React.ReactNode
  ssr?: boolean
}

type Module<TProps> = { default: React.ComponentType<TProps> } | React.ComponentType<TProps>

export default function dynamic<TProps, TRef = unknown>(
  load: () => Promise<Module<TProps>>,
  options: DynamicOptions = {},
) {
  const DynamicComponent = forwardRef<TRef, TProps>(function DynamicComponent(props, ref) {
    const [Loaded, setLoaded] = useState<React.ComponentType<TProps> | null>(null)
    const [error, setError] = useState<Error | null>(null)

    useEffect(() => {
      let cancelled = false
      setError(null)
      setLoaded(null)
      void load()
        .then((module) => {
          if (cancelled) return
          setLoaded(() => ('default' in module ? module.default : module))
        })
        .catch((caught: unknown) => {
          if (cancelled) return
          setError(
            caught instanceof Error ? caught : new Error('This part of Lyra could not be loaded.'),
          )
        })
      return () => {
        cancelled = true
      }
    }, [])

    if (error) {
      const retry = () => window.location.reload()
      return (
        <>
          {options.error?.(error, retry) ?? (
            <div role="alert" className="rounded-md border border-border px-4 py-3 text-sm">
              <p>This part of Lyra could not be loaded.</p>
              <button type="button" className="mt-3 underline" onClick={retry}>
                Retry
              </button>
            </div>
          )}
        </>
      )
    }
    if (!Loaded) return <>{options.loading?.() ?? null}</>
    return <Loaded {...(props as TProps)} ref={ref as never} />
  })

  DynamicComponent.displayName = 'DynamicComponent'
  return DynamicComponent
}
