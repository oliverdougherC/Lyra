/**
 * KaTeX's vendor stylesheet, imported once and into its own `katex` cascade layer. It comes
 * first, ahead of `globals.css` (which arrives with `AppRoot` below), because the layer order
 * is fixed by the first statement the browser meets and an earlier import could fix an order
 * with no `katex` in it. See `styles/katex.css` for what the layering holds and why this is a
 * stylesheet of its own rather than an import inside `globals.css`.
 */
import '@/styles/katex.css'

import React from 'react'
import { createRoot } from 'react-dom/client'

import GlobalErrorFallback from '@/app/global-error'
import { AppRoot } from '@/app/root'
import { initializeRuntimeConfig, recoverDesktopBackend } from '@/lib/runtime'
import { RouterProvider } from '@/router/hooks'

class GlobalBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  state = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  render() {
    if (this.state.error) {
      return <GlobalErrorFallback error={this.state.error} retry={() => window.location.reload()} />
    }
    return this.props.children
  }
}

async function bootstrap() {
  await initializeRuntimeConfig()

  const container = document.getElementById('root')
  if (!container) throw new Error('The application root element was not found.')

  createRoot(container).render(
    <GlobalBoundary>
      <RouterProvider>
        <AppRoot />
      </RouterProvider>
    </GlobalBoundary>,
  )
}

void bootstrap().catch((error: unknown) => {
  const container = document.getElementById('root')
  if (!container) return
  const safeError = error instanceof Error ? error : new Error('Desktop startup failed.')
  const retry = async () => {
    const restarted = await recoverDesktopBackend()
    if (restarted) window.location.reload()
    return restarted
  }
  createRoot(container).render(<GlobalErrorFallback error={safeError} retry={retry} />)
})
