import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'

import { StreamingMarkdown } from '@/components/chat/streaming-markdown'
import {
  classifyExternalHref,
  ExternalLinkInterceptor,
  openExternalUrl,
} from '@/lib/external-links'

vi.mock('sonner', () => ({ toast: { error: vi.fn() } }))

beforeEach(() => {
  delete window.__TAURI__
  delete window.__TAURI_INTERNALS__
  vi.unstubAllEnvs()
  vi.mocked(toast.error).mockClear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
})

describe('external links', () => {
  it('classifies packaged-external and internal links distinctly', () => {
    expect(classifyExternalHref('/classes/7')).toEqual({ kind: 'internal' })
    expect(classifyExternalHref('#main-content')).toEqual({ kind: 'internal' })
    expect(classifyExternalHref('javascript:alert(1)')).toEqual({ kind: 'blocked' })
    expect(classifyExternalHref('https://example.com/docs')).toEqual({
      kind: 'external',
      url: 'https://example.com/docs',
    })
  })

  it('routes markdown links through the typed Tauri command', async () => {
    const invoke = vi.fn().mockResolvedValue(undefined)
    window.__TAURI_INTERNALS__ = { invoke }

    render(
      <>
        <ExternalLinkInterceptor />
        <StreamingMarkdown content="[Read more](https://example.com/docs)" />
      </>,
    )

    await userEvent.click(screen.getByRole('link', { name: 'Read more' }))

    expect(invoke).toHaveBeenCalledWith('open_external_url', {
      url: 'https://example.com/docs',
    })
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('intercepts target blank source links without changing route', async () => {
    const invoke = vi.fn().mockResolvedValue(undefined)
    window.__TAURI_INTERNALS__ = { invoke }

    render(
      <>
        <ExternalLinkInterceptor />
        <a href="https://example.com" target="_blank" rel="noreferrer">
          Open source
        </a>
      </>,
    )

    await userEvent.click(screen.getByRole('link', { name: 'Open source' }))

    expect(invoke).toHaveBeenCalledWith('open_external_url', {
      url: 'https://example.com/',
    })
  })

  it('blocks unsafe markdown anchors without invoking Tauri', async () => {
    const invoke = vi.fn().mockResolvedValue(undefined)
    window.__TAURI_INTERNALS__ = { invoke }

    render(
      <>
        <ExternalLinkInterceptor />
        <StreamingMarkdown content="[Unsafe](http://127.0.0.1:8000/admin)" />
      </>,
    )

    await userEvent.click(screen.getByRole('link', { name: 'Unsafe' }))

    expect(invoke).not.toHaveBeenCalled()
    expect(toast.error).toHaveBeenCalledWith('Lyra can only open public http or https links.')
  })

  it('restores the prior focus when a rejected link is clicked', async () => {
    render(
      <>
        <ExternalLinkInterceptor />
        <button type="button">Keep focus here</button>
        <a href="http://127.0.0.1:8000/admin">Unsafe link</a>
      </>,
    )

    const button = screen.getByRole('button', { name: 'Keep focus here' })
    button.focus()
    await userEvent.click(screen.getByRole('link', { name: 'Unsafe link' }))

    expect(button).toHaveFocus()
    expect(toast.error).toHaveBeenCalledWith('Lyra can only open public http or https links.')
  })

  it('falls back to the browser only in explicit development mode', async () => {
    vi.stubEnv('DEV', true)
    const open = vi.fn().mockReturnValue(window)
    vi.spyOn(window, 'open').mockImplementation(open)

    await expect(openExternalUrl('https://example.com')).resolves.toBeUndefined()
    expect(open).toHaveBeenCalledWith('https://example.com/', '_blank', 'noopener,noreferrer')
  })
})
