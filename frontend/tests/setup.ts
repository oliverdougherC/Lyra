import '@testing-library/jest-dom/vitest'

import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// jsdom implements neither of these, and Radix primitives and the reduced-motion checks in
// the reveal cascade both touch them on mount. Without the stubs every component test fails on the same
// unrelated error, which buries the assertion that actually matters.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

// Assigned rather than stubbed: `unstubGlobals` restores globals after every test, so a
// stub set up here survives only the first one and later tests in the same file fail on a
// missing ResizeObserver depending on which effects they happen to trigger.
if (!('ResizeObserver' in globalThis)) {
  globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver
}

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {}
}

window.__LYRA_BOOTSTRAP__ = { apiBase: 'http://127.0.0.1:8000' }

// The runtime bootstrap falls back to loopback in tests, and jsdom will really fetch it, so
// a test that forgets to stub the API passes or fails depending on whether a backend happens
// to be listening. This guard turns that silent dependency into an immediate failure naming
// the URL. Assigned rather than stubbed for the same reason as ResizeObserver above; a test
// that needs fetch stubs it with `vi.stubGlobal('fetch', ...)`, and `unstubGlobals` restores
// the guard afterwards.
globalThis.fetch = ((input: RequestInfo | URL) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  throw new Error('unstubbed fetch in test: ' + url)
}) as typeof fetch

afterEach(() => {
  cleanup()
})
