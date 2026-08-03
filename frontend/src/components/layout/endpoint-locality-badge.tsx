'use client'

import { useRouter } from 'next/navigation'

import { Skeleton } from '@/components/ui/skeleton'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useSettings } from '@/lib/hooks/use-settings'
import { cn } from '@/lib/utils'
import type { SettingsRead } from '@/types'

type Locality = {
  label: string
  textClass: string
  hollow: boolean
  tooltip: string
}

function resolveLocality(settings: SettingsRead): Locality {
  if (!settings.endpoint_url) {
    return {
      label: 'Not connected',
      textClass: 'text-text-tertiary',
      hollow: true,
      tooltip: 'No tutor endpoint is configured yet. Open settings to add one.',
    }
  }
  const host = settings.endpoint_host ?? 'the configured endpoint'
  if (settings.endpoint_is_local) {
    return {
      label: 'Local',
      textClass: 'text-success-text',
      hollow: false,
      tooltip: `Your tutor model runs on ${host}. Nothing leaves this machine.`,
    }
  }
  if (settings.remote_ack) {
    return {
      label: 'Remote',
      textClass: 'text-info-text',
      hollow: false,
      tooltip: `Your document text is sent to ${host}.`,
    }
  }
  return {
    label: 'Remote, unconfirmed',
    textClass: 'text-danger-text',
    hollow: false,
    tooltip: `${host} is not on this machine, and you have not acknowledged sending your documents there.`,
  }
}

/**
 * The standing answer to "where is my data going". Always visible in the header. Colour
 * is never the only signal: the label text differs in every state.
 */
export function EndpointLocalityBadge() {
  const router = useRouter()
  const { data: settings, isPending, isError } = useSettings()

  if (isPending) return <Skeleton className="h-7 w-32 rounded-sm" />

  const locality = isError
    ? {
        label: 'Not connected',
        textClass: 'text-text-tertiary',
        hollow: true,
        tooltip: 'Lyra could not read your settings. Open settings to check the endpoint.',
      }
    : resolveLocality(settings)

  return (
    <Tooltip>
      <TooltipTrigger
        onClick={() => router.push('/settings')}
        className={cn(
          'inline-flex items-center gap-2 rounded-md border bg-card px-2.5 py-1.5 text-[13px] font-medium',
          'transition-colors duration-150 hover:bg-muted focus-visible:ring-ring',
          'focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none',
          locality.textClass,
        )}
        aria-label={`Tutor endpoint: ${locality.label}. Open settings.`}
      >
        <span
          aria-hidden
          className={cn(
            'size-2 rounded-full border',
            locality.hollow ? 'border-current bg-transparent' : 'border-current bg-current',
          )}
        />
        {locality.label}
      </TooltipTrigger>
      <TooltipContent>{locality.tooltip}</TooltipContent>
    </Tooltip>
  )
}
