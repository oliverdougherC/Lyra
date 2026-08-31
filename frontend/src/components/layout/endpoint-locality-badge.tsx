'use client'

import Link from '@/router/link'
import { Globe, HardDrive } from 'lucide-react'

import { useSettings } from '@/lib/hooks/use-settings'
import { cn } from '@/lib/utils'

/**
 * The privacy pillar's one ambient affordance (ui-overhaul 2.7): a quiet header chip that
 * always says where the model runs. Local is the nominal state and prints in muted ink;
 * Remote is the exception worth noticing, because the student's documents leave the machine,
 * so it takes a red pencil and names the host. Either way it links into Settings, where the
 * endpoint is actually changed. It becomes more important as web research and tools arrive,
 * which is why it is ambient rather than buried in Settings.
 */

const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '0.0.0.0', '::1', ''])

function locality(endpointUrl: string | null | undefined): { local: boolean; host: string } {
  if (!endpointUrl) return { local: true, host: '' }
  try {
    const { hostname } = new URL(endpointUrl)
    const local = LOCAL_HOSTS.has(hostname) || hostname.endsWith('.local')
    return { local, host: hostname }
  } catch {
    // An unparseable endpoint is not a claim of privacy: fail toward "remote" so a
    // misconfiguration never reads as safe.
    return { local: false, host: endpointUrl }
  }
}

export function EndpointLocalityBadge({ className }: { className?: string }) {
  const { data: settings } = useSettings()
  // Until settings load there is nothing honest to say, so the chip holds its space silently.
  if (!settings) return null

  const { local, host } = locality(settings.endpoint_url)

  return (
    <Link
      href="/settings"
      title={
        local
          ? 'The model runs on this machine. Your documents stay local.'
          : `The model runs at ${host}. Your documents are sent there.`
      }
      className={cn(
        'focus-visible:ring-ring inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors duration-150 focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none',
        local
          ? 'border-border text-text-secondary hover:text-text-primary'
          : 'border-danger-text/40 text-danger-text hover:border-danger-text',
        className,
      )}
    >
      {local ? (
        <HardDrive aria-hidden className="size-3.5" />
      ) : (
        <Globe aria-hidden className="size-3.5" />
      )}
      {local ? (
        <span>Local</span>
      ) : (
        <span className="flex items-baseline gap-1">
          Remote
          <span className="text-text-tertiary tabular-nums">· {host}</span>
        </span>
      )}
    </Link>
  )
}
