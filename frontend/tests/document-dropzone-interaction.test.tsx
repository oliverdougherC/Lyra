import { fireEvent, render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'

import { DocumentDropzone } from '@/components/documents/document-dropzone'

it('keeps drop feedback while moving across children and clears it on cancellation', () => {
  render(<DocumentDropzone />)
  const well = screen.getByRole('group', { name: 'Upload documents' })
  const button = screen.getByRole('button', { name: 'Upload files' })
  fireEvent.dragEnter(well)
  fireEvent.dragEnter(button)
  fireEvent.dragLeave(button)
  expect(screen.getByText('Drop to upload')).toBeInTheDocument()
  fireEvent.dragLeave(well)
  expect(screen.getByText('Upload files')).toBeInTheDocument()

  fireEvent.dragEnter(well)
  fireEvent.dragEnd(well)
  expect(screen.getByText('Upload files')).toBeInTheDocument()
})
