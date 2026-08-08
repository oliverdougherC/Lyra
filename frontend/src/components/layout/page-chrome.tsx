'use client'

/**
 * How a route claims the window and the header bar.
 *
 * Most routes are pages: a padded column inside the shell's rounded surface, which is the
 * right frame for a list of classes or a settings form. A workspace is not a page. It is
 * two panes of the student's own material that want every pixel, and wrapping it in a
 * page inside a card inside an inset shell left the actual reading column at 41% of the
 * width and 72% of the height of a 13-inch laptop.
 *
 * So a workspace route asks for the whole window with `useFullBleed`, and puts its title
 * and its actions in the app header with `HeaderCrumb` and `HeaderActions` rather than
 * spending another 48px row on a title of its own. Portals rather than state: the header
 * is above the route in the tree, the actions are JSX that changes identity every render,
 * and lifting them through context would re-render the shell on every keystroke below it.
 *
 * `useImmersiveChrome` goes one step further and takes the navigation away too, for the
 * one route where the chrome is not the point: a draft is a page of writing, and the
 * sidebar and header around it are two borders and 320px of somewhere else to be. It is
 * the route's own state - a route that asks for it and then unmounts hands the chrome
 * back, so no student can navigate away and find the application missing.
 */

import { ChevronRight } from 'lucide-react'
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useSyncExternalStore,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'

export const HEADER_CRUMB_SLOT = 'lyra-header-crumb'
export const HEADER_ACTIONS_SLOT = 'lyra-header-actions'

const FullBleedContext = createContext<(bleed: boolean) => void>(() => undefined)
const ImmersiveContext = createContext<(immersive: boolean) => void>(() => undefined)

export function FullBleedProvider({
  onChange,
  children,
}: {
  onChange: (bleed: boolean) => void
  children: ReactNode
}) {
  return <FullBleedContext.Provider value={onChange}>{children}</FullBleedContext.Provider>
}

export function ImmersiveProvider({
  onChange,
  children,
}: {
  onChange: (immersive: boolean) => void
  children: ReactNode
}) {
  return <ImmersiveContext.Provider value={onChange}>{children}</ImmersiveContext.Provider>
}

/**
 * Ask the shell for the whole window while this route is mounted.
 *
 * Takes a boolean rather than being a bare call, because the solution route is a workspace
 * only once there is something to work in: while it is segmenting, or waiting at the
 * review gate, it is an ordinary centred page and wants the ordinary frame.
 */
export function useFullBleed(enabled: boolean): void {
  const setBleed = useContext(FullBleedContext)
  useEffect(() => {
    setBleed(enabled)
    return () => setBleed(false)
  }, [enabled, setBleed])
}

/**
 * Slide the sidebar and the header away for as long as this route asks for it.
 *
 * Full bleed removes the frame around a route; this removes the application around it, so
 * what is left is the student's own page and the tools that act on it. Cleared on unmount
 * for the same reason `useFullBleed` is, and it matters more here: navigating out of a
 * route with no navigation on screen must not be something a student has to discover how
 * to undo. The preference that turns it on belongs to the route, which is what lets it
 * survive a session without following anyone onto a page that has no way back.
 */
export function useImmersiveChrome(enabled: boolean): void {
  const setImmersive = useContext(ImmersiveContext)
  useEffect(() => {
    setImmersive(enabled)
    return () => setImmersive(false)
  }, [enabled, setImmersive])
}

function Slot({ id, children }: { id: string; children: ReactNode }) {
  const subscribe = useCallback(() => () => undefined, [])
  const read = useCallback(() => document.getElementById(id), [id])
  // Through `useSyncExternalStore` rather than an effect: the target is a fact about the
  // document, not state this component owns, and the server has no document to look in.
  // The element is stable once it exists, so the snapshot is stable too.
  const target = useSyncExternalStore(subscribe, read, () => null)

  return target ? createPortal(children, target) : null
}

/**
 * The last breadcrumb, supplied by the route rather than derived from the path.
 *
 * The slot is `display: contents`, so these land as items of the breadcrumb's own flex row
 * and pick up its spacing without a wrapper.
 */
export function HeaderCrumb({ children }: { children: string }) {
  return (
    <Slot id={HEADER_CRUMB_SLOT}>
      <ChevronRight aria-hidden className="hidden size-3.5 shrink-0 text-text-tertiary sm:block" />
      <span aria-current="page" className="truncate font-medium" title={children}>
        {children}
      </span>
    </Slot>
  )
}

/** The route's own actions, rendered in the header beside the class-level ones. */
export function HeaderActions({ children }: { children: ReactNode }) {
  return <Slot id={HEADER_ACTIONS_SLOT}>{children}</Slot>
}
