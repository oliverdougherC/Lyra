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

  it('normalizes protocol-relative and mixed-case public http links', () => {
    expect(classifyExternalHref('//example.com/docs', 'http://127.0.0.1:4179/#/')).toEqual({
      kind: 'external',
      url: 'http://example.com/docs',
    })
    expect(classifyExternalHref('HTTPS://EXAMPLE.COM/Docs')).toEqual({
      kind: 'external',
      url: 'https://example.com/Docs',
    })
  })

  it('blocks reserved IPv6 ranges and IPv4-mapped private or loopback addresses', () => {
    for (const href of [
      'https://[::]/',
      'https://[::1]/',
      'https://[fe80::1]/',
      'https://[fc00::1]/',
      'https://[2001:db8::1]/',
      'https://[ff02::1]/',
      'https://[::ffff:127.0.0.1]/',
      'https://[::ffff:10.1.2.3]/',
    ]) {
      expect(classifyExternalHref(href)).toEqual({ kind: 'blocked' })
    }
  })

  it('allows public IPv6 addresses', () => {
    expect(classifyExternalHref('https://[2606:4700:4700::1111]/')).toEqual({
      kind: 'external',
      url: 'https://[2606:4700:4700::1111]/',
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

  it('keeps native open failures distinct from blocked links', async () => {
    const invoke = vi.fn().mockRejectedValue(new Error('native opener failed'))
    window.__TAURI_INTERNALS__ = { invoke }

    render(
      <>
        <ExternalLinkInterceptor />
        <button type="button">Keep focus here</button>
        <a href="https://example.com/docs">Public link</a>
      </>,
    )

    const button = screen.getByRole('button', { name: 'Keep focus here' })
    button.focus()
    await userEvent.click(screen.getByRole('link', { name: 'Public link' }))

    expect(invoke).toHaveBeenCalledWith('open_external_url', {
      url: 'https://example.com/docs',
    })
    expect(button).toHaveFocus()
    expect(toast.error).toHaveBeenCalledWith('That link could not be opened.')
  })

  it('maps structured native policy denials back to the blocked category', async () => {
    const invoke = vi.fn().mockRejectedValue({
      code: 'blocked',
      message: 'Lyra can only open public http or https links.',
    })
    window.__TAURI_INTERNALS__ = { invoke }

    render(
      <>
        <ExternalLinkInterceptor />
        <a href="https://example.com/docs">Revalidated link</a>
      </>,
    )

    await userEvent.click(screen.getByRole('link', { name: 'Revalidated link' }))

    expect(toast.error).toHaveBeenCalledWith('Lyra can only open public http or https links.')
    expect(toast.error).not.toHaveBeenCalledWith('That link could not be opened.')
  })

  it('falls back to the browser only in explicit development mode', async () => {
    vi.stubEnv('DEV', true)
    const open = vi.fn().mockReturnValue(window)
    vi.spyOn(window, 'open').mockImplementation(open)

    await expect(openExternalUrl('https://example.com')).resolves.toBeUndefined()
    expect(open).toHaveBeenCalledWith('https://example.com/', '_blank', 'noopener,noreferrer')
  })
})
