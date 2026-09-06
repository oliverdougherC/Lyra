import { act, fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { Composer } from '@/components/chat/composer'

/**
 * PLA-318: `disabled={streaming}` makes the browser blur the focused textarea on an
 * Enter-send, and before the fix nothing restored focus when the turn settled -- a
 * keyboard-only student had to Tab/click back into the composer after every answer.
 *
 * The contract under test: a keyboard send remembers that the textarea owned focus,
 * restores it (with preventScroll) when streaming ends, and never steals focus from a
 * control the student deliberately moved to while the answer streamed. Button sends
 * never trigger restoration at all.
 */

const noop = () => {}

type Props = Parameters<typeof Composer>[0]

function baseProps(overrides: Partial<Props> = {}): Props {
  return {
    value: 'What is a derivative?',
    onChange: noop,
    onSend: noop,
    onStop: noop,
    streaming: false,
    disabledReason: null,
    ...overrides,
  }
}

/** The composer plus one unrelated interactive control, as a chat pane would have. */
function Chrome(props: Props) {
  return (
    <>
      <Composer {...props} />
      <button type="button">Open sources</button>
    </>
  )
}

function textarea(): HTMLTextAreaElement {
  return screen.getByLabelText('Message Lyra')
}

/**
 * Real browsers blur a control the moment it becomes disabled; jsdom neither blurs on
 * disable nor honors blur() once disabled. Blur immediately BEFORE the streaming
 * rerender so the end state (disabled textarea, focus on body) matches what a real
 * browser produces, and the assertions test the production restore path, not a jsdom
 * quirk.
 */
function blurAsBrowserWould(node: HTMLElement) {
  if (document.activeElement === node) {
    act(() => node.blur())
  }
}

describe('Composer focus across a streaming turn (PLA-318)', () => {
  it('keyboard send: focus returns to the textarea when streaming ends', () => {
    const onSend = vi.fn()
    const { rerender } = render(<Chrome {...baseProps({ onSend })} />)

    const node = textarea()
    act(() => node.focus())
    expect(node).toHaveFocus()

    fireEvent.keyDown(node, { key: 'Enter' })
    expect(onSend).toHaveBeenCalledTimes(1)

    blurAsBrowserWould(node)
    rerender(<Chrome {...baseProps({ onSend, streaming: true })} />)
    expect(node).toBeDisabled()
    expect(node).not.toHaveFocus()

    rerender(<Chrome {...baseProps({ onSend, streaming: false })} />)
    expect(node).toHaveFocus()
  })

  it.each([false, true])(
    'waits for delayed transcript readiness before restoring focus (moved: %s)',
    (moved) => {
      const onSend = vi.fn()
      const { rerender } = render(<Chrome {...baseProps({ onSend })} />)
      const node = textarea()
      act(() => node.focus())
      fireEvent.keyDown(node, { key: 'Enter' })
      blurAsBrowserWould(node)
      rerender(<Chrome {...baseProps({ onSend, streaming: true, blocked: true })} />)
      rerender(
        <Chrome
          {...baseProps({ onSend, streaming: false, blocked: true, value: 'Next question' })}
        />,
      )
      expect(node).toBeDisabled()
      expect(node).not.toHaveFocus()
      const other = screen.getByRole('button', { name: 'Open sources' })
      if (moved) act(() => other.focus())
      rerender(<Chrome {...baseProps({ onSend, blocked: false, value: 'Next question' })} />)
      expect(node).toBeEnabled()
      expect(node).toHaveValue('Next question')
      expect(onSend).toHaveBeenCalledTimes(1)
      if (moved) {
        expect(other).toHaveFocus()
        expect(node).not.toHaveFocus()
      } else {
        expect(node).toHaveFocus()
      }
    },
  )

  it('restores with preventScroll so the transcript does not jump', () => {
    const onSend = vi.fn()
    const { rerender } = render(<Chrome {...baseProps({ onSend })} />)

    const node = textarea()
    act(() => node.focus())
    fireEvent.keyDown(node, { key: 'Enter' })

    blurAsBrowserWould(node)
    rerender(<Chrome {...baseProps({ onSend, streaming: true })} />)

    const focusSpy = vi.spyOn(node, 'focus')
    rerender(<Chrome {...baseProps({ onSend, streaming: false })} />)
    expect(focusSpy).toHaveBeenCalledWith({ preventScroll: true })
  })

  it('does not steal focus from a control the student focused during streaming', () => {
    const onSend = vi.fn()
    const { rerender } = render(<Chrome {...baseProps({ onSend })} />)

    const node = textarea()
    act(() => node.focus())
    fireEvent.keyDown(node, { key: 'Enter' })

    blurAsBrowserWould(node)
    rerender(<Chrome {...baseProps({ onSend, streaming: true })} />)

    // The student deliberately moves to another interactive control mid-stream.
    const other = screen.getByRole('button', { name: 'Open sources' })
    act(() => other.focus())
    expect(other).toHaveFocus()

    rerender(<Chrome {...baseProps({ onSend, streaming: false })} />)
    expect(other).toHaveFocus()
    expect(node).not.toHaveFocus()
  })

  it('mouse/button send never triggers focus restoration', () => {
    const onSend = vi.fn()
    const { rerender } = render(<Chrome {...baseProps({ onSend })} />)

    fireEvent.click(screen.getByRole('button', { name: 'Send message' }))
    expect(onSend).toHaveBeenCalledTimes(1)

    const node = textarea()
    blurAsBrowserWould(node)
    rerender(<Chrome {...baseProps({ onSend, streaming: true })} />)

    rerender(<Chrome {...baseProps({ onSend, streaming: false })} />)
    expect(node).not.toHaveFocus()
  })

  it('preserves Shift+Enter (newline, no send) and empty-draft Enter (no send)', () => {
    const onSend = vi.fn()
    const { rerender } = render(<Chrome {...baseProps({ onSend })} />)

    const node = textarea()
    act(() => node.focus())
    fireEvent.keyDown(node, { key: 'Enter', shiftKey: true })
    expect(onSend).not.toHaveBeenCalled()

    rerender(<Chrome {...baseProps({ onSend, value: '   ' })} />)
    fireEvent.keyDown(node, { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
  })
})

describe('Composer IME input (PLA-407)', () => {
  it.each([{ isComposing: true }, { keyCode: 229 }])(
    'does not send candidate confirmation %o',
    (ime) => {
      const onSend = vi.fn()
      render(<Composer {...baseProps({ onSend })} />)
      fireEvent.keyDown(textarea(), { key: 'Enter', ...ime })
      expect(onSend).not.toHaveBeenCalled()
      fireEvent.keyDown(textarea(), { key: 'Enter' })
      expect(onSend).toHaveBeenCalledTimes(1)
    },
  )
})
