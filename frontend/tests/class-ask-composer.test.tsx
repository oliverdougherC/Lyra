import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ClassAskComposer } from '@/components/classes/class-ask-composer'
import { readChatDraft, resetChatDraftMemory, writeChatDraft } from '@/lib/chat-draft-store'

afterEach(() => vi.useRealTimers())
beforeEach(() => {
  localStorage.clear()
  resetChatDraftMemory()
})

const ideas = ['Explain convolution', 'Walk me through Fourier series']
let keySeq = 0
function setup(onSend = vi.fn(), draftKey = `lyra:class:1:ask-question#${keySeq++}`) {
  const view = render(
    <ClassAskComposer
      className="Signals"
      draftKey={draftKey}
      suggestions={ideas}
      onSend={onSend}
    />,
  )
  return {
    box: screen.getByRole('textbox', { name: 'Ask about Signals' }),
    onSend,
    view,
    draftKey,
  }
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

  it('blocks a second ClassAsk view while the first send settles after navigation', async () => {
    let finish!: () => void
    const onSend = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve
        }),
    )
    const draftKey = 'lyra:class:6:ask-question'
    const first = setup(onSend, draftKey)
    fireEvent.change(first.box, { target: { value: 'One question' } })
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
    first.view.unmount()
    const second = setup(onSend, draftKey)
    expect(second.box).toHaveValue('One question')
    expect(screen.getByRole('button', { name: 'Ask' })).toBeDisabled()
    await act(async () => finish())
    expect(second.box).toHaveValue('')
    expect(onSend).toHaveBeenCalledTimes(1)
    expect(readChatDraft(`class-ask:${draftKey}`)).toBeNull()
  })

  it('keeps a newer question in the returned view without a retirement warning', async () => {
    let finish!: () => void
    const onSend = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve
        }),
    )
    const draftKey = 'lyra:class:9:ask-question'
    const first = setup(onSend, draftKey)
    fireEvent.change(first.box, { target: { value: 'Question A' } })
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
    first.view.unmount()
    const second = setup(onSend, draftKey)
    fireEvent.change(second.box, { target: { value: 'Question B' } })
    await act(async () => finish())
    expect(second.box).toHaveValue('Question B')
    expect(readChatDraft(`class-ask:${draftKey}`)?.value).toBe('Question B')
    expect(screen.queryByText(/saved copy could not be removed/i)).not.toBeInTheDocument()
    expect(onSend).toHaveBeenCalledTimes(1)
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

  it('restores a half-typed question after the page unmounts and returns', () => {
    const { box, view, draftKey } = setup()
    fireEvent.change(box, { target: { value: 'Set up a study plan' } })
    view.unmount()
    sessionStorage.clear()
    resetChatDraftMemory()
    setup(vi.fn(), draftKey)
    expect(screen.getByRole('textbox', { name: 'Ask about Signals' })).toHaveValue(
      'Set up a study plan',
    )
  })

  it('drops the stored draft once the question sends', async () => {
    const { box, view, draftKey } = setup(vi.fn().mockResolvedValue(undefined))
    fireEvent.change(box, { target: { value: 'Already asked' } })
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
    await act(async () => {})
    view.unmount()
    resetChatDraftMemory()
    setup(vi.fn(), draftKey)
    expect(screen.getByRole('textbox', { name: 'Ask about Signals' })).toHaveValue('')
  })

  it('settles an unchanged restored question after successful send', async () => {
    const draftKey = 'lyra:class:7:ask-question'
    writeChatDraft(`class-ask:${draftKey}`, 'Restored class question')
    resetChatDraftMemory()
    const { box, view, onSend } = setup(vi.fn().mockResolvedValue(undefined), draftKey)
    expect(box).toHaveValue('Restored class question')
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
    await act(async () => {})
    expect(onSend).toHaveBeenCalledExactlyOnceWith('Restored class question')
    view.unmount()
    resetChatDraftMemory()
    setup(vi.fn(), draftKey)
    expect(screen.getByRole('textbox', { name: 'Ask about Signals' })).toHaveValue('')
    expect(readChatDraft(`class-ask:${draftKey}`)?.value ?? '').toBe('')
  })

  it('warns when accepted text cannot be retired and blocks an unchanged resend after remount', async () => {
    const draftKey = 'lyra:class:8:ask-question'
    writeChatDraft(`class-ask:${draftKey}`, 'Already submitted')
    resetChatDraftMemory()
    const remove = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new DOMException('Storage refused', 'SecurityError')
    })
    try {
      const first = setup(vi.fn().mockResolvedValue(undefined), draftKey)
      expect(first.box).toHaveValue('Already submitted')
      fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
      expect(await screen.findByRole('alert')).toHaveTextContent('saved copy could not be removed')
      first.view.unmount()
      const navigated = setup(vi.fn(), draftKey)
      expect(navigated.box).toHaveValue('')
      navigated.view.unmount()
      resetChatDraftMemory()
      const second = setup(vi.fn(), draftKey)
      expect(second.box).toHaveValue('')
      expect(screen.getByRole('button', { name: 'Ask' })).toBeDisabled()
      expect(second.onSend).not.toHaveBeenCalled()
    } finally {
      remove.mockRestore()
    }
  })
})
