'use client'

import { forwardRef } from 'react'

import { useRouter } from '@/router/hooks'

type LinkProps = Omit<React.AnchorHTMLAttributes<HTMLAnchorElement>, 'href'> & {
  href: string
}

function isInternalHref(href: string): boolean {
  return href.startsWith('/')
}

const Link = forwardRef<HTMLAnchorElement, LinkProps>(function Link(
  { href, onClick, target, rel, ...props },
  ref,
) {
  let router: ReturnType<typeof useRouter> | null = null
  try {
    router = useRouter()
  } catch {
    router = null
  }
  const internal = isInternalHref(href)
  const browserHref = internal ? `/#${href}` : href

  return (
    <a
      {...props}
      ref={ref}
      href={browserHref}
      target={target}
      rel={rel}
      onClick={(event) => {
        onClick?.(event)
        if (
          event.defaultPrevented ||
          !internal ||
          target === '_blank' ||
          event.metaKey ||
          event.ctrlKey ||
          event.shiftKey ||
          event.altKey ||
          event.button !== 0
        ) {
          return
        }
        event.preventDefault()
        router?.push(href)
      }}
    />
  )
})

export default Link
