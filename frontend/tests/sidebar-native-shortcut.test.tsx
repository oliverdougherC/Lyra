import { act, render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { SidebarProvider, useSidebar } from '@/components/ui/sidebar'

it('uses the same sidebar toggle for native chat and removes the listener on unmount', () => {
  function State() {
    const { state } = useSidebar()
    return <span>{state}</span>
  }
  const onOpenChange = vi.fn()
  const view = render(
    <SidebarProvider>
      <State />
    </SidebarProvider>,
  )
  act(() => window.dispatchEvent(new Event('lyra:toggle-sidebar')))
  expect(screen.getByText('collapsed')).toBeInTheDocument()
  act(() => window.dispatchEvent(new KeyboardEvent('keydown', { key: 'b', metaKey: true })))
  expect(screen.getByText('expanded')).toBeInTheDocument()
  view.unmount()
  const controlled = render(
    <SidebarProvider onOpenChange={onOpenChange}>
      <State />
    </SidebarProvider>,
  )
  controlled.unmount()
  act(() => window.dispatchEvent(new Event('lyra:toggle-sidebar')))
  expect(onOpenChange).not.toHaveBeenCalled()
})
