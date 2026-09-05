'use client'

import { useEffect } from 'react'
import { toast } from 'sonner'

const OPEN_EXTERNAL_URL_COMMAND = 'open_external_url' as const
const BLOCKED_LINK_MESSAGE = 'Lyra can only open public http or https links.'
const OPEN_FAILED_MESSAGE = 'That link could not be opened.'
const ABSOLUTE_SCHEME = /^[a-zA-Z][a-zA-Z\d+.-]*:/
let pendingExternalLinkFocus: Element | null = null
const trustedDownloads = new WeakMap<HTMLAnchorElement, string>()

type ExternalHrefDecision =
  { kind: 'internal' } | { kind: 'blocked' } | { kind: 'external'; url: string }

type TauriCore = {
  invoke?: (command: string, args?: Record<string, unknown>) => Promise<unknown>
}

declare global {
  interface Window {
    __TAURI__?: { core?: TauriCore }
    __TAURI_INTERNALS__?: TauriCore
  }
}

class ExternalLinkError extends Error {
  constructor(readonly kind: 'blocked' | 'unavailable' | 'open-failed') {
    super(
      kind === 'blocked'
        ? BLOCKED_LINK_MESSAGE
        : kind === 'unavailable'
          ? OPEN_FAILED_MESSAGE
          : OPEN_FAILED_MESSAGE,
    )
  }
}

function tauriInvoke() {
  return window.__TAURI__?.core?.invoke ?? window.__TAURI_INTERNALS__?.invoke
}

function nativeExternalLinkError(error: unknown): ExternalLinkError {
  if (typeof error === 'object' && error !== null && 'code' in error && error.code === 'blocked') {
    return new ExternalLinkError('blocked')
  }
  return new ExternalLinkError('open-failed')
}

function readBaseUrl() {
  return typeof window === 'undefined' ? 'https://tauri.localhost/' : window.location.href
}

function stripIpv6Brackets(hostname: string) {
  return hostname.startsWith('[') && hostname.endsWith(']') ? hostname.slice(1, -1) : hostname
}

function parseIpv4(hostname: string): number[] | null {
  const parts = hostname.split('.')
  if (parts.length !== 4) return null
  const octets = parts.map((part) => Number.parseInt(part, 10))
  if (octets.some((octet, index) => String(octet) !== parts[index] || octet < 0 || octet > 255)) {
    return null
  }
  return octets
}

function isSafePublicIpv4Octets(octets: number[]): boolean {
  const [first, second] = octets
  return !(
    first === 0 ||
    first === 10 ||
    first === 127 ||
    first >= 240 ||
    first === 169 ||
    (first === 100 && second >= 64 && second <= 127) ||
    (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && second === 168) ||
    (first === 198 && (second === 18 || second === 19))
  )
}

function isSafePublicIpv4(hostname: string): boolean {
  const octets = parseIpv4(hostname)
  return octets ? isSafePublicIpv4Octets(octets) : false
}

function parseIpv6Part(part: string): number[] | null {
  if (!part) return []

  const segments: number[] = []
  for (const token of part.split(':')) {
    if (!token) return null
    if (token.includes('.')) {
      const octets = parseIpv4(token)
      if (!octets) return null
      segments.push((octets[0] << 8) | octets[1], (octets[2] << 8) | octets[3])
      continue
    }
    if (!/^[\da-f]{1,4}$/i.test(token)) {
      return null
    }
    segments.push(Number.parseInt(token, 16))
  }
  return segments
}

function parseIpv6(hostname: string) {
  const normalized = stripIpv6Brackets(hostname).toLowerCase()
  if (!normalized.includes(':')) return null

  const halves = normalized.split('::')
  if (halves.length > 2) return null

  const head = parseIpv6Part(halves[0] ?? '')
  const tail = parseIpv6Part(halves[1] ?? '')
  if (!head || !tail) return null
  if (head.length + tail.length > 8) return null

  const segments =
    halves.length === 2
      ? [...head, ...Array.from({ length: 8 - head.length - tail.length }, () => 0), ...tail]
      : head

  if (segments.length !== 8) return null

  const ipv4Mapped =
    segments[0] === 0 &&
    segments[1] === 0 &&
    segments[2] === 0 &&
    segments[3] === 0 &&
    segments[4] === 0 &&
    segments[5] === 0xffff
      ? [segments[6] >> 8, segments[6] & 0xff, segments[7] >> 8, segments[7] & 0xff]
      : null

  return { segments, ipv4Mapped }
}

function isSafePublicIpv6(hostname: string): boolean {
  const parsed = parseIpv6(hostname)
  if (!parsed) return false
  if (parsed.ipv4Mapped) return isSafePublicIpv4Octets(parsed.ipv4Mapped)

  const [first, second] = parsed.segments
  return !(
    parsed.segments.every((segment) => segment === 0) ||
    (parsed.segments.slice(0, 7).every((segment) => segment === 0) && parsed.segments[7] === 1) ||
    (first & 0xff00) === 0xff00 ||
    (first === 0x2001 && second === 0x0db8) ||
    (first === 0x2001 && second <= 0x01ff) ||
    (first & 0xfe00) === 0xfc00 ||
    (first & 0xffc0) === 0xfe80 ||
    (first & 0xffc0) === 0xfec0
  )
}

function isSafePublicHost(hostname: string): boolean {
  const lower = stripIpv6Brackets(hostname).toLowerCase()
  if (
    !lower ||
    lower === 'localhost' ||
    lower.endsWith('.localhost') ||
    lower.endsWith('.local') ||
    lower.endsWith('.internal')
  ) {
    return false
  }
  if (lower.includes(':')) return isSafePublicIpv6(lower)
  if (/^\d+\.\d+\.\d+\.\d+$/.test(lower)) return isSafePublicIpv4(lower)
  return lower.includes('.')
}

export function classifyExternalHref(
  rawHref: string | null,
  baseUrl = readBaseUrl(),
): ExternalHrefDecision {
  if (!rawHref) return { kind: 'internal' }
  if (
    (rawHref.startsWith('/') && !rawHref.startsWith('//')) ||
    rawHref.startsWith('./') ||
    rawHref.startsWith('../')
  ) {
    return { kind: 'internal' }
  }
  if (rawHref.startsWith('#') || rawHref.startsWith('?')) {
    return { kind: 'internal' }
  }
  if (rawHref.trim() !== rawHref) {
    return { kind: 'blocked' }
  }

  const hasScheme = ABSOLUTE_SCHEME.test(rawHref)
  if (!hasScheme && !rawHref.startsWith('//')) {
    return { kind: 'internal' }
  }

  let url: URL
  try {
    url = new URL(rawHref, baseUrl)
  } catch {
    return { kind: 'blocked' }
  }

  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    return { kind: 'blocked' }
  }
  if (url.origin === new URL(baseUrl).origin) {
    return { kind: 'internal' }
  }
  if (url.username || url.password || !isSafePublicHost(url.hostname)) {
    return { kind: 'blocked' }
  }

  return { kind: 'external', url: url.toString() }
}

async function dispatchExternalUrl(url: string): Promise<void> {
  const invoke = tauriInvoke()
  if (invoke) {
    try {
      await invoke(OPEN_EXTERNAL_URL_COMMAND, { url })
    } catch (error) {
      throw nativeExternalLinkError(error)
    }
    return
  }

  if (import.meta.env.DEV || import.meta.env.VITE_API_BASE) {
    const opened = window.open(url, '_blank', 'noopener,noreferrer')
    if (opened) return
  }

  throw new ExternalLinkError('unavailable')
}

export async function openExternalUrl(rawHref: string): Promise<void> {
  const decision = classifyExternalHref(rawHref)
  if (decision.kind !== 'external') {
    throw new ExternalLinkError('blocked')
  }
  try {
    await dispatchExternalUrl(decision.url)
  } catch (error) {
    if (error instanceof ExternalLinkError) throw error
    throw new ExternalLinkError('open-failed')
  }
}

function findAnchor(target: EventTarget | null): HTMLAnchorElement | null {
  const element =
    target instanceof Element ? target : target instanceof Node ? target.parentElement : null
  if (!(element instanceof Element)) return null
  const anchor = element.closest('a[href]')
  return anchor instanceof HTMLAnchorElement ? anchor : null
}

function restoreFocus(target: Element | null) {
  if (target instanceof HTMLElement && document.contains(target)) {
    const refocus = () => {
      if (document.contains(target)) {
        target.focus({ preventScroll: true })
      }
    }
    refocus()
    queueMicrotask(refocus)
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(refocus)
    }
  }
}

function rememberExternalLinkFocus(event: MouseEvent | PointerEvent) {
  const anchor = findAnchor(event.target)
  if (!anchor) {
    pendingExternalLinkFocus = null
    return
  }
  const decision = classifyExternalHref(anchor.getAttribute('href'))
  if (decision.kind === 'internal') {
    pendingExternalLinkFocus = null
    return
  }
  if (pendingExternalLinkFocus === null) {
    pendingExternalLinkFocus = document.activeElement
  }
}

/** Only app-created byte downloads may pass the navigation guard's blob restriction. */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  trustedDownloads.set(link, url)
  document.body.appendChild(link)
  try {
    link.click()
  } finally {
    trustedDownloads.delete(link)
    link.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
  }
}

function handleLinkActivation(event: MouseEvent) {
  if (event.defaultPrevented || event.button === 2) return

  const anchor = findAnchor(event.target)
  if (!anchor) return
  if (anchor.hasAttribute('download') && trustedDownloads.get(anchor) === anchor.href) return

  const decision = classifyExternalHref(anchor.getAttribute('href'))
  if (decision.kind === 'internal') return

  const activeElement = pendingExternalLinkFocus ?? document.activeElement
  pendingExternalLinkFocus = null
  event.preventDefault()
  event.stopPropagation()

  if (decision.kind === 'blocked') {
    restoreFocus(activeElement)
    toast.error(BLOCKED_LINK_MESSAGE)
    return
  }

  void dispatchExternalUrl(decision.url).catch((error) => {
    restoreFocus(activeElement)
    toast.error(
      error instanceof ExternalLinkError && error.kind === 'blocked'
        ? BLOCKED_LINK_MESSAGE
        : OPEN_FAILED_MESSAGE,
    )
  })
}

export function ExternalLinkInterceptor() {
  useEffect(() => {
    document.addEventListener('pointerdown', rememberExternalLinkFocus, true)
    document.addEventListener('mousedown', rememberExternalLinkFocus, true)
    document.addEventListener('click', handleLinkActivation, true)
    document.addEventListener('auxclick', handleLinkActivation, true)
    return () => {
      pendingExternalLinkFocus = null
      document.removeEventListener('pointerdown', rememberExternalLinkFocus, true)
      document.removeEventListener('mousedown', rememberExternalLinkFocus, true)
      document.removeEventListener('click', handleLinkActivation, true)
      document.removeEventListener('auxclick', handleLinkActivation, true)
    }
  }, [])

  return null
}
