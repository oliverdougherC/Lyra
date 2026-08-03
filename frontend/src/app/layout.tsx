import type { Metadata } from 'next'
import { DM_Sans, Fraunces, JetBrains_Mono, Source_Serif_4 } from 'next/font/google'

import { AppShell } from '@/components/layout/app-shell'
import { Providers } from '@/app/providers'
import { THEME_INIT_SCRIPT } from '@/lib/theme'
import '@/styles/globals.css'

const dmSans = DM_Sans({
  variable: '--font-dm-sans',
  subsets: ['latin'],
  weight: ['400', '500', '600', '700'],
  display: 'swap',
})

const fraunces = Fraunces({
  variable: '--font-fraunces',
  subsets: ['latin'],
  weight: ['500', '600', '700'],
  display: 'swap',
})

const jetbrainsMono = JetBrains_Mono({
  variable: '--font-jetbrains-mono',
  subsets: ['latin'],
})

/**
 * The tutor's reading voice. A text serif rather than a math face: answers are prose with
 * emphasis, and a single-weight math font leaves the browser to synthesize every bold run.
 * KaTeX keeps its own fonts for the math itself.
 */
const sourceSerif = Source_Serif_4({
  variable: '--font-source-serif',
  subsets: ['latin'],
  weight: ['400', '600'],
  style: ['normal', 'italic'],
  display: 'swap',
})

export const metadata: Metadata = {
  title: 'Lyra',
  description: 'A local-first study tutor grounded in your own course material',
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html
      lang="en"
      className={`${dmSans.variable} ${fraunces.variable} ${jetbrainsMono.variable} ${sourceSerif.variable} h-full antialiased`}
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      {/* The shell owns the viewport: the rail and header never scroll, route content
          scrolls inside `main`. */}
      <body className="flex h-full flex-col overflow-hidden">
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  )
}
