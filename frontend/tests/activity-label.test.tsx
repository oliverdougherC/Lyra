import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { activityLabel } from '@/components/chat/activity-label'
import { ThinkingIndicator } from '@/components/chat/thinking-indicator'

const activity = (tool: string, ok = true) => [
  { tool, ok, label: 'Private answer or reasoning text' },
]

afterEach(() => vi.useRealTimers())

describe('observable activity labels', () => {
  it('reports completed tools without quoting model text or arguments', () => {
    expect(activityLabel(activity('read_section'))).toBe('Read a section')
    expect(activityLabel(activity('search_course_material'))).toBe('Searched course material')
    expect(activityLabel(activity('propose_revision'))).toBe('Proposed a revision')
  })

  it('uses only the latest result and falls back for unknown or unsuccessful tools', () => {
    expect(activityLabel([...activity('read_section'), ...activity('new_tool')])).toBe('Thinking')
    expect(activityLabel(activity('read_section', false))).toBe('Thinking')
    expect(activityLabel(activity('toString'))).toBe('Thinking')
    expect(activityLabel([])).toBe('Thinking')
    expect(activityLabel([], 'reviewing_documents')).toBe('Looking through your material')
  })

  it('coalesces rapid changes and never rotates labels without new evidence', () => {
    vi.useFakeTimers()
    const { rerender } = render(<ThinkingIndicator label="Thinking" startedAt={1} />)
    rerender(<ThinkingIndicator label="Read a section" startedAt={1} />)
    act(() => vi.advanceTimersByTime(400))
    rerender(<ThinkingIndicator label="Searched the web" startedAt={1} />)
    expect(screen.getByText('Thinking')).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(400))
    expect(screen.getByText('Searched the web')).toBeInTheDocument()
    expect(screen.queryByText('Read a section')).not.toBeInTheDocument()
    act(() => vi.advanceTimersByTime(10000))
    expect(screen.getByText('Searched the web')).toBeInTheDocument()
  })

  it('discards pending labels on retry and unmount', () => {
    vi.useFakeTimers()
    const { rerender, unmount } = render(<ThinkingIndicator label="Read a section" startedAt={1} />)
    rerender(<ThinkingIndicator label="Searched the web" startedAt={1} />)
    rerender(<ThinkingIndicator label="Thinking" startedAt={2} />)
    expect(screen.getByText('Thinking')).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(1000))
    expect(screen.queryByText('Searched the web')).not.toBeInTheDocument()
    unmount()
    expect(vi.getTimerCount()).toBe(0)
  })
})
