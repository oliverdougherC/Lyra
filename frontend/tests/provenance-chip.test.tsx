import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ProvenanceChip } from '@/components/solutions/provenance-chip'
import type { Provenance } from '@/types'

function entry(overrides: Partial<Provenance> = {}): Provenance {
  return {
    chunk_id: 1,
    document_id: 2,
    page_number: 92,
    label: null,
    filename: 'kuttler.pdf',
    section_path: null,
    bbox: null,
    ...overrides,
  }
}

describe('ProvenanceChip', () => {
  it('names the section a step came from, not only its page', () => {
    // The Phase 3 definition of done: an answer cites the section by its path rather than by
    // a page number alone. The page stays, because that is what a reader turns to.
    render(<ProvenanceChip entries={[entry({ section_path: 'Vector Spaces / Subspaces' })]} />)

    expect(screen.getByText('kuttler.pdf, Vector Spaces / Subspaces, page 92')).toBeInTheDocument()
  })

  it('keeps the levels that locate something and drops the ones the filename already said', () => {
    // A four-level path spends most of its width on the book's outermost divisions. The whole
    // path stays reachable on the chip's title attribute.
    const path = 'Matrices / Determinants / Cofactors / The Cofactor Expansion'
    render(<ProvenanceChip entries={[entry({ section_path: path })]} />)

    expect(
      screen.getByText('kuttler.pdf, Cofactors / The Cofactor Expansion, page 92'),
    ).toBeInTheDocument()
    expect(screen.getByRole('listitem')).toHaveAttribute('title', path)
  })

  it('degrades to filename and page when the source has no structure', () => {
    // Every document indexed before sections existed, and every one that has none. An old
    // solution is not a broken one.
    render(<ProvenanceChip entries={[entry()]} />)

    expect(screen.getByText('kuttler.pdf, page 92')).toBeInTheDocument()
  })

  it('still cites a deleted document by the page number that is still true', () => {
    render(
      <ProvenanceChip entries={[entry({ filename: null, chunk_id: null, section_path: null })]} />,
    )

    expect(screen.getByText('A deleted document, page 92')).toBeInTheDocument()
  })

  it('renders nothing for a step that was not grounded in anything', () => {
    const { container } = render(
      <ProvenanceChip entries={[entry({ filename: null, page_number: null })]} />,
    )

    expect(container).toBeEmptyDOMElement()
  })
})
