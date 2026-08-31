import 'katex/dist/katex.min.css'

import React from 'react'
import { createRoot } from 'react-dom/client'

import GlobalErrorFallback from '@/app/global-error'
import { AppRoot } from '@/app/root'
import { initializeRuntimeConfig } from '@/lib/runtime'
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
  createRoot(container).render(
    <GlobalErrorFallback error={safeError} retry={() => window.location.reload()} />,
  )
})
