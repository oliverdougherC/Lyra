import { useState, type ReactNode } from 'react'
import { ChevronRight } from 'lucide-react'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { useRouteAnchor } from '@/router/hooks'

/** Keep maintenance status mounted so a new failure can reveal itself while closed. */
export function SettingsDisclosure({
  title,
  description,
  children,
  attention = false,
  anchors = [],
}: {
  title: string
  description: string
  children: ReactNode
  attention?: boolean
  anchors?: string[]
}) {
  const [open, setOpen] = useState(false)
  const anchor = useRouteAnchor()
  const expanded = open || attention || anchors.includes(anchor ?? '')
  return (
    <section className="border-border/70 border-t pt-6">
      <Collapsible open={expanded} onOpenChange={setOpen}>
        <CollapsibleTrigger
          aria-label={title}
          aria-disabled={attention || undefined}
          className="focus-visible:ring-ring flex w-full items-center justify-between gap-3 rounded-md text-left focus-visible:ring-2 focus-visible:outline-none [&[data-state=open]>svg]:rotate-90"
        >
          <span className="min-w-0">
            <span className="font-heading block text-xl leading-tight font-medium tracking-tight">
              {title}
            </span>
            <span className="text-text-secondary mt-1 block text-sm">{description}</span>
          </span>
          <ChevronRight aria-hidden className="text-text-tertiary size-5 shrink-0" />
        </CollapsibleTrigger>
        <CollapsibleContent forceMount hidden={!expanded}>
          <div className="mt-5">{children}</div>
        </CollapsibleContent>
      </Collapsible>
    </section>
  )
}
