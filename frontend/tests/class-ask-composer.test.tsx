import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ClassAskComposer } from '@/components/classes/class-ask-composer'

afterEach(() => vi.useRealTimers())

const ideas = ['Explain convolution', 'Walk me through Fourier series']
function setup(onSend = vi.fn()) {
  render(<ClassAskComposer className="Signals" suggestions={ideas} onSend={onSend} />)
  return { box: screen.getByRole('textbox', { name: 'Ask about Signals' }), onSend }
}

describe('class opening composer', () => {
  it('rotates ideas, pauses on focus, and never overwrites a draft', () => {
    vi.useFakeTimers()
    const { box } = setup()
    act(() => vi.advanceTimersByTime(6000))
    expect(screen.getByText(ideas[1])).toBeVisible()
    fireEvent.focus(box)
    act(() => vi.advanceTimersByTime(12000))
    expect(screen.getByText(ideas[1])).toBeVisible()
    fireEvent.change(box, { target: { value: 'My own question' } })
    fireEvent.blur(box)
    act(() => vi.advanceTimersByTime(12000))
    expect(box).toHaveValue('My own question')
    expect(screen.queryByText(ideas[1])).not.toBeInTheDocument()
  })

  it('lets the student stop cycling and insert an idea without sending it', () => {
    vi.useFakeTimers()
    const { box, onSend } = setup()
    fireEvent.click(screen.getByRole('button', { name: 'Pause suggestions' }))
    act(() => vi.advanceTimersByTime(12000))
    expect(screen.getByText(ideas[0])).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: /Use this idea/ }))
    expect(box).toHaveValue(ideas[0])
    expect(box).toHaveFocus()
    expect(onSend).not.toHaveBeenCalled()
  })

  it('keeps ideas still for reduced motion', () => {
    vi.useFakeTimers()
    vi.spyOn(window, 'matchMedia').mockReturnValue({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    } as unknown as MediaQueryList)
    setup()
    act(() => vi.advanceTimersByTime(12000))
    expect(screen.getByText(ideas[0])).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Pause suggestions' })).not.toBeInTheDocument()
    vi.restoreAllMocks()
  })

  it('rejects empty sends, preserves multiline/IME input, and sends once during handoff', async () => {
    let finish!: () => void
    const onSend = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve
        }),
    )
    const { box } = setup(onSend)
    expect(screen.getByRole('button', { name: 'Ask' })).toBeDisabled()
    fireEvent.change(box, { target: { value: '  Why?\nHow?  ' } })
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })
    fireEvent.keyDown(box, { key: 'Enter', isComposing: true })
    fireEvent.keyDown(box, { key: 'Enter', keyCode: 229 })
    expect(onSend).not.toHaveBeenCalled()
    fireEvent.keyDown(box, { key: 'Enter' })
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(onSend).toHaveBeenCalledExactlyOnceWith('Why?\nHow?')
    await act(async () => finish())
  })

  it('retains the question and makes retry available when navigation fails', async () => {
    const onSend = vi.fn().mockRejectedValue(new Error('Chunk unavailable'))
    const { box } = setup(onSend)
    fireEvent.change(box, { target: { value: 'Explain convolution' } })
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Your question is still here')
    expect(box).toHaveValue('Explain convolution')
    expect(screen.getByRole('button', { name: 'Ask' })).toBeEnabled()
  })
})
