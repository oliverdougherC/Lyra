'use client'

import '@/styles/globals.css'

import { Providers } from '@/app/providers'
import { AppShell } from '@/components/layout/app-shell'
import { AppRoutes } from '@/router/app-routes'

export function AppRoot() {
  return (
    <Providers>
      <AppShell>
        <AppRoutes />
      </AppShell>
    </Providers>
  )
}
