'use client'

import { BookOpen, ExternalLink, Globe2 } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { useDraftSources } from '@/lib/hooks/use-drafts'
import type { DraftSource } from '@/types'

/** Course and web evidence shared by the writer and class agent. */
export function SourceLedger({ classId }: { classId: number }) {
  const query = useDraftSources(classId)

  if (query.isPending) {
    return (
      <div className="flex flex-col gap-2" aria-busy="true">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    )
  }
  if (query.isError) {
    return (
      <div className="space-y-3">
        <p className="text-danger-text text-sm">The source ledger could not be loaded.</p>
        <Button variant="outline" size="sm" onClick={() => void query.refetch()}>
          Retry
        </Button>
      </div>
    )
  }

  const sources = query.data ?? []
  if (sources.length === 0) {
    return (
      <div className="space-y-2">
        <p className="text-text-primary text-sm font-medium">No sources yet</p>
        <p className="text-text-tertiary text-sm">
          Course readings and fetched web pages appear here once Lyra relies on them.
        </p>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-5">
      <p className="text-text-secondary text-sm">
        Every source Lyra relied on, with the exact excerpts kept for claim checking.
      </p>
      <SourceGroup
        title="Course material"
        sources={sources.filter((one) => one.source_type === 'course')}
      />
      <SourceGroup
        title="Web research"
        sources={sources.filter((one) => one.source_type === 'web')}
      />
    </div>
  )
}

function SourceGroup({ title, sources }: { title: string; sources: DraftSource[] }) {
  if (sources.length === 0) return null
  return (
    <section aria-label={title}>
      <h3 className="eyebrow text-text-tertiary mb-2">{title}</h3>
      <ol className="flex flex-col gap-3">
        {sources.map((source) => (
          <li
            key={source.id}
            id={`source-${source.id}`}
            tabIndex={-1}
            className="border-border rounded-md border p-3"
          >
            <div className="flex items-start gap-2">
              {source.source_type === 'web' ? (
                <Globe2 className="text-text-tertiary mt-0.5 size-4 shrink-0" />
              ) : (
                <BookOpen className="text-text-tertiary mt-0.5 size-4 shrink-0" />
              )}
              <div className="min-w-0 flex-1">
                <p className="text-text-primary text-sm font-medium">{source.title}</p>
                {source.accessed_at ? (
                  <p className="text-text-tertiary mt-0.5 text-xs">
                    Accessed {new Date(source.accessed_at).toLocaleDateString()}
                  </p>
                ) : null}
              </div>
              {source.url ? (
                <Button asChild variant="ghost" size="icon" className="size-7 shrink-0">
                  <a
                    href={source.url}
                    target="_blank"
                    rel="noreferrer"
                    aria-label={`Open ${source.title}`}
                  >
                    <ExternalLink className="size-3.5" />
                  </a>
                </Button>
              ) : null}
            </div>
            {source.excerpts.length > 0 ? (
              <ul className="border-border/70 mt-3 flex flex-col gap-2 border-l pl-3">
                {source.excerpts.map((excerpt) => (
                  <li key={excerpt.id}>
                    {excerpt.section_ref ? (
                      <p className="text-text-tertiary text-[11px]">For {excerpt.section_ref}</p>
                    ) : null}
                    <blockquote className="text-text-secondary text-xs leading-5">
                      {excerpt.excerpt}
                    </blockquote>
                  </li>
                ))}
              </ul>
            ) : null}
          </li>
        ))}
      </ol>
    </section>
  )
}
