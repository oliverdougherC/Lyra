'use client'

import { memo } from 'react'
import Markdown from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import rehypeKatex from 'rehype-katex'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'

import { cn } from '@/lib/utils'

const REMARK_PLUGINS = [remarkGfm, remarkMath]
const REHYPE_PLUGINS = [rehypeKatex, rehypeHighlight]

/**
 * Memoized on `content` so a stream that appends one token does not re-parse the markdown
 * for every other message on screen.
 */
export const StreamingMarkdown = memo(function StreamingMarkdown({
  content,
  streaming = false,
}: {
  content: string
  streaming?: boolean
}) {
  return (
    <div
      className={cn(
        'text-sm leading-6 text-text-primary [&_a]:underline [&_a]:underline-offset-2',
        '[&_code]:rounded-sm [&_code]:bg-muted [&_code]:px-1 [&_code]:font-mono [&_code]:text-[0.9em]',
        '[&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:border [&_pre]:bg-muted [&_pre]:p-3 [&_pre_.hljs]:bg-transparent [&_pre_.hljs]:p-0',
        '[&_h1]:mt-4 [&_h1]:mb-2 [&_h1]:font-heading [&_h1]:text-lg [&_h1]:font-medium [&_h1]:tracking-tight',
        '[&_h2]:mt-4 [&_h2]:mb-2 [&_h2]:font-heading [&_h2]:text-base [&_h2]:font-medium [&_h2]:tracking-tight',
        '[&_h3]:mt-3 [&_h3]:mb-1.5 [&_h3]:font-heading [&_h3]:text-sm [&_h3]:font-medium [&_h3]:tracking-tight',
        '[&_p]:my-2 [&_ol]:my-2 [&_ul]:my-2 [&_ol]:list-decimal [&_ul]:list-disc [&_ol]:pl-5 [&_ul]:pl-5',
        '[&_li]:my-0.5 [&_blockquote]:my-3 [&_blockquote]:border-l-2 [&_blockquote]:border-accent-primary [&_blockquote]:bg-accent-surface/50 [&_blockquote]:py-1 [&_blockquote]:pr-3 [&_blockquote]:pl-3 [&_blockquote]:text-text-secondary',
        '[&_table]:my-3 [&_table]:w-full [&_table]:border-collapse [&_th]:border [&_td]:border [&_th]:border-border [&_td]:border-border [&_th]:bg-muted [&_th]:px-2 [&_td]:px-2 [&_th]:py-1 [&_td]:py-1',
        '[&_.katex]:text-text-primary [&_.katex-display]:my-4',
      )}
    >
      <Markdown remarkPlugins={REMARK_PLUGINS} rehypePlugins={REHYPE_PLUGINS}>
        {content}
      </Markdown>
      {streaming ? (
        <span
          className="bg-accent-primary ml-0.5 inline-block h-4 w-[2px] translate-y-0.5 motion-safe:animate-pulse"
          aria-hidden
        />
      ) : null}
    </div>
  )
})
