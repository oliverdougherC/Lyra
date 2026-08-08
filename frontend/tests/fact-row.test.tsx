import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { FactRow } from '@/components/profile/fact-row'
import type { FactRead } from '@/types'

function fact(overrides: Partial<FactRead> = {}): FactRead {
  return {
    id: 1,
    class_id: 1,
    kind: 'topic',
    label: 'Topic',
    value: 'Fourier series',
    confidence: 'high',
    confirmed: false,
    rejected: false,
    edited: false,
    source_document_id: 1,
    source_filename: 'homework_1.pdf',
    sources: ['homework_1.pdf'],
    source_writer_id: null,
    source_excerpt_id: null,
    source_title: null,
    source_url: null,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderRow(overrides: Partial<FactRead> = {}) {
  render(<FactRow fact={fact(overrides)} busy={false} onCorrect={() => {}} onResolve={() => {}} />)
}

/**
 * A fact is now one claim about the class rather than one line per file, so what a row has
 * to say about its origin changed: how many documents back it, not which upload happened to
 * mention it first.
 */
describe('FactRow', () => {
  it('names the one document a fact came from', () => {
    renderRow({ sources: ['homework_1.pdf'] })

    expect(screen.getByText('From homework_1.pdf')).toBeInTheDocument()
  })

  it('counts the documents when several agree', () => {
    // The count is the point. Four filenames stacked in a row would say less and take four
    // lines to say it, and the student cannot act on any of them individually.
    renderRow({ sources: ['homework_1.pdf', 'homework_2.pdf', 'homework_7.pdf'] })

    expect(screen.getByText('In 3 documents')).toBeInTheDocument()
    expect(screen.queryByText(/^From /)).not.toBeInTheDocument()
  })

  it('links a web-evidenced proposal to its source', () => {
    renderRow({
      confidence: 'low',
      source_document_id: null,
      source_filename: 'Method reference',
      sources: ['Method reference'],
      source_writer_id: 4,
      source_excerpt_id: 8,
      source_title: 'Method reference',
      source_url: 'https://example.com/method',
    })

    expect(screen.getByRole('link', { name: 'From Method reference' })).toHaveAttribute(
      'href',
      'https://example.com/method',
    )
  })

  it('says nothing about sources when a fact has none', () => {
    renderRow({ sources: [], source_filename: null, source_document_id: null })

    expect(screen.queryByText(/document/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/^From /)).not.toBeInTheDocument()
  })

  it('asks for confirmation only while a low-confidence fact is unconfirmed', () => {
    renderRow({ confidence: 'low' })

    expect(screen.getByText('Not used until you confirm this')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeInTheDocument()
  })

  it('leaves a corroborated fact alone', () => {
    // Two documents state it independently, so the backend already made it active. Asking
    // the student to confirm what the material has already vouched for is the busywork this
    // whole change exists to remove.
    renderRow({ confidence: 'high', sources: ['homework_1.pdf', 'homework_2.pdf'] })

    expect(screen.queryByText('Not used until you confirm this')).not.toBeInTheDocument()
  })
})
