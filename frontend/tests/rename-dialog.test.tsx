import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { RenameDialog } from '@/components/classes/rename-dialog'
import { ApiError } from '@/lib/api'

function renderDialog(overrides: Partial<Parameters<typeof RenameDialog>[0]> = {}) {
  const onRename = overrides.onRename ?? vi.fn().mockResolvedValue(undefined)
  const onOpenChange = overrides.onOpenChange ?? vi.fn()
  render(
    <RenameDialog
      target={{ id: 7, name: 'Homework 4' }}
      title="Rename solution set"
      description="Give it a name."
      label="Name"
      pending={false}
      {...overrides}
      onRename={onRename}
      onOpenChange={onOpenChange}
    />,
  )
  return { onRename, onOpenChange }
}

describe('RenameDialog', () => {
  it('opens on the current name with saving disabled', () => {
    renderDialog()

    expect(screen.getByLabelText('Name')).toHaveValue('Homework 4')
    // Nothing has changed yet, so there is nothing to save.
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('saves the trimmed name', async () => {
    const { onRename, onOpenChange } = renderDialog()

    await userEvent.clear(screen.getByLabelText('Name'))
    await userEvent.type(screen.getByLabelText('Name'), '  Week 4 homework  ')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onRename).toHaveBeenCalledWith(7, 'Week 4 homework'))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('submits on Enter, since the dialog holds one field', async () => {
    const { onRename } = renderDialog()

    await userEvent.clear(screen.getByLabelText('Name'))
    await userEvent.type(screen.getByLabelText('Name'), 'Fourier week{Enter}')

    await waitFor(() => expect(onRename).toHaveBeenCalledWith(7, 'Fourier week'))
  })

  it('refuses a blank name rather than clearing the old one', async () => {
    const { onRename } = renderDialog()

    await userEvent.clear(screen.getByLabelText('Name'))
    await userEvent.type(screen.getByLabelText('Name'), '   ')

    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(onRename).not.toHaveBeenCalled()
  })

  it('keeps the dialog open and shows why when the rename fails', async () => {
    const onRename = vi.fn().mockRejectedValue(new ApiError(409, 'That name is taken.'))
    const { onOpenChange } = renderDialog({ onRename })

    await userEvent.clear(screen.getByLabelText('Name'))
    await userEvent.type(screen.getByLabelText('Name'), 'Taken{Enter}')

    expect(await screen.findByRole('alert')).toHaveTextContent('That name is taken.')
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
  })
})
