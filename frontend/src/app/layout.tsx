import type { Metadata } from 'next'
import { Caveat, Cinzel, EB_Garamond, JetBrains_Mono } from 'next/font/google'

import { AppShell } from '@/components/layout/app-shell'
import { Providers } from '@/app/providers'
import { THEME_INIT_SCRIPT } from '@/lib/theme'
import '@/styles/globals.css'

/**
 * Inscription. Cinzel is a Roman capital face drawn from stone-cut letterforms; it names
 * things and never navigates (design system section 4). Loaded at 600, the one weight the
 * nameplates use, with the carved text-shadow applied in globals.css.
 */
const cinzel = Cinzel({
  variable: '--font-cinzel',
  subsets: ['latin'],
  weight: ['600'],
  display: 'swap',
})

/**
 * Print. EB Garamond carries everything read: body at 15 to 16.5px, labels as letterspaced
 * caps, numerals tabular where they align. The italic exists solely as a mathematics
 * fallback where KaTeX is not in play; the interface itself never slants (section 4).
 */
const ebGaramond = EB_Garamond({
  variable: '--font-eb-garamond',
  subsets: ['latin'],
  weight: ['400', '500', '600'],
  style: ['normal', 'italic'],
  display: 'swap',
})

/**
 * The hand. Caveat renders only what the student did, chose, or must do (section 5): margin
 * notes, the chosen depth, the place they are in. Loaded at 500, the weight the pen writes
 * in.
 */
const caveat = Caveat({
  variable: '--font-caveat',
  subsets: ['latin'],
  weight: ['500'],
  display: 'swap',
})

/** Code, and only code. Status and numerals are print, never mono (section 4). */
const jetbrainsMono = JetBrains_Mono({
  variable: '--font-jetbrains-mono',
  subsets: ['latin'],
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
      className={`${cinzel.variable} ${ebGaramond.variable} ${caveat.variable} ${jetbrainsMono.variable} h-full antialiased`}
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
