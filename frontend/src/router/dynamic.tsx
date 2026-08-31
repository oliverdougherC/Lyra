'use client'

import { forwardRef, useEffect, useState } from 'react'

type DynamicOptions = {
  loading?: () => React.ReactNode
  ssr?: boolean
}

type Module<TProps> = { default: React.ComponentType<TProps> } | React.ComponentType<TProps>

export default function dynamic<TProps, TRef = unknown>(
  load: () => Promise<Module<TProps>>,
  options: DynamicOptions = {},
) {
  const DynamicComponent = forwardRef<TRef, TProps>(function DynamicComponent(props, ref) {
    const [Loaded, setLoaded] = useState<React.ComponentType<TProps> | null>(null)

    useEffect(() => {
      let cancelled = false
      void load().then((module) => {
        if (cancelled) return
        setLoaded(() => ('default' in module ? module.default : module))
      })
      return () => {
        cancelled = true
      }
    }, [])

    if (!Loaded) return <>{options.loading?.() ?? null}</>
    return <Loaded {...(props as TProps)} ref={ref as never} />
  })

  DynamicComponent.displayName = 'DynamicComponent'
  return DynamicComponent
}
