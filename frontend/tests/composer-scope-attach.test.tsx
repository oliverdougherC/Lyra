import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState, type ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  useWorkspaceAttach,
  WorkspaceAttachProvider,
  WorkspaceContextChip,
} from '@/components/agent/workspace-attach'
import { Composer } from '@/components/chat/composer'
import { SourceContext } from '@/components/chat/source-context'
import { TooltipProvider } from '@/components/ui/tooltip'
import { api } from '@/lib/api'
import * as runtime from '@/lib/runtime'
import type { AgentWorkspaceRead, DocumentRead } from '@/types'

/**
 * PLA-403: the composer used to carry two wide labeled controls - "All material" and
 * "Attach folder" - permanently on the left of the input, which took the row from the text
 * and made the well read as a toolbar. The redesign keeps both capabilities but gives them
 * the smallest honest weight: a compact scope pill that discloses the full picker, and a
 * 24px attach icon that carries its name in a tooltip and an accessible label. The input
 * row is the composer; these marks ride its line.
 *
 * jsdom does no layout, so the "normal and narrow" layout contract is asserted on the
 * structure that makes the layout correct: the input is the flex-1 element, the context
 * row is shrink-0 and bounded, the scope pill is width-capped so a long filename
 * truncates, and the attach affordance is a fixed-size icon rather than a labeled button.
 */

const CLASS_ID = 21

function doc(id: number, filename: string, state: DocumentRead['state'] = 'ready'): DocumentRead {
  return {
    id,
    class_id: CLASS_ID,
    filename,
    mime: 'application/pdf',
    byte_size: 2048,
    state,
    stage_detail: null,
    pages_total: 3,
    pages_done: 3,
    pages_skipped: 0,
    pages_failed: 0,
    recognize: false,
    error_message: null,
    created_at: '2026-08-04 00:00:00',
  }
}

function workspace(overrides: Partial<AgentWorkspaceRead> = {}): AgentWorkspaceRead {
  return {
    id: 1,
    class_id: CLASS_ID,
    root_path: '/tmp/starter',
    display_name: 'starter',
    read_enabled: true,
    change_proposals_enabled: false,
    commands_enabled: false,
    created_at: '2026-09-02T00:00:00Z',
    updated_at: '2026-09-02T00:00:00Z',
    ...overrides,
  }
}

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      {/* The app wraps every route in one provider; the tests mirror that seam. */}
      <TooltipProvider delayDuration={300}>
        <WorkspaceAttachProvider classId={CLASS_ID}>{children}</WorkspaceAttachProvider>
      </TooltipProvider>
    </QueryClientProvider>
  )
  return wrapper
}

/** The full composer, as the class chat page renders it: both context marks present. */
function ComposerWithControls({ documents }: { documents: DocumentRead[] }) {
  return (
    <Composer
      value=""
      onChange={() => {}}
      onSend={() => {}}
      onStop={() => {}}
      streaming={false}
      disabledReason={null}
      sourceControl={<SourceContext documents={documents} selectedId={null} onSelect={() => {}} />}
      workspaceControl={<WorkspaceContextChip />}
    />
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'getAgentWorkspace').mockResolvedValue(null)
  vi.spyOn(api, 'attachAgentWorkspace').mockResolvedValue(workspace())
  vi.spyOn(api, 'detachAgentWorkspace').mockResolvedValue(undefined)
  vi.spyOn(runtime, 'desktopFolderPickerAvailable').mockReturnValue(true)
  vi.spyOn(runtime, 'pickDesktopWorkspaceDirectory').mockResolvedValue('/tmp/starter')
})

describe('the composer keeps the input line dominant (PLA-403)', () => {
  it('gives the text entry the whole input line, with the context marks a quiet line beneath', () => {
    render(<ComposerWithControls documents={[doc(1, 'lecture_5.pdf')]} />, {
      wrapper: createWrapper(),
    })

    const textarea = screen.getByLabelText('Message Lyra')
    expect(textarea).toHaveClass('flex-1')

    // The context marks live on their own line beneath the type: the input line is not
    // crowded by them at any width, so the marks never take the row from the text.
    const marks = document.querySelector('[data-source-control]')
    expect(marks).not.toBeNull()
    expect(marks!.contains(textarea)).toBe(false)
    // ...and the input line carries exactly the entry and its send.
    expect(textarea.parentElement!.querySelector('button')).not.toBeNull()
    expect(marks!.querySelector('#message-composer')).toBeNull()

    // The well hugs its content: no reserved control row forcing empty canvas.
    const well = textarea.closest('.rounded-2xl')
    expect(well).not.toBeNull()
    expect(well!.className).not.toMatch(/min-h-/)

    // The send action stays the only prominent control: one button, round, on the line.
    expect(screen.getByRole('button', { name: 'Send message' })).toHaveClass('rounded-full')
  })

  it('reduces the folder attach to a compact icon affordance with an accessible name', () => {
    render(<ComposerWithControls documents={[doc(1, 'lecture_5.pdf')]} />, {
      wrapper: createWrapper(),
    })

    // The old wide labeled button is gone...
    expect(screen.queryByText('Attach folder')).toBeNull()
    // ...and the affordance is a fixed-size icon whose name lives in its label and
    // tooltip rather than in a word of visible text.
    const attach = screen.getByRole('button', { name: 'Attach a folder' })
    expect(attach).toHaveAttribute('data-attach-folder')
    expect(attach.textContent).toBe('')
    expect(attach).toHaveClass('size-6')

    // No permanent toolbar: at rest the well carries exactly its three controls - the
    // scope mark, the attach icon, and the send.
    const well = screen.getByLabelText('Message Lyra').closest('.rounded-2xl')
    expect(well!.querySelectorAll('button')).toHaveLength(3)
    expect(well!.querySelector('[data-slot="dropdown-menu-content"]')).toBeNull()
    expect(document.querySelector('[data-slot="popover-content"]')).toBeNull()
  })

  it('caps the scope pill so a long filename truncates instead of taking the row', () => {
    // A filename that would eat a narrow input row if it could grow: the pill is
    // width-capped at both the base and the sm breakpoint, and its label truncates.
    render(
      <ComposerWithControls
        documents={[doc(1, 'a-very-long-document-name-that-keeps-going.pdf')]}
      />,
      { wrapper: createWrapper() },
    )

    const chip = screen.getByRole('button', { name: /Choose what Lyra reads/ })
    expect(chip).toHaveClass('max-w-[6.5rem]', 'sm:max-w-[9rem]')
    expect(chip).toHaveTextContent('All material')
    const label = chip.querySelector('span')
    expect(label).toHaveClass('truncate')
  })

  it('carries no context marks at all for a composer with none to offer', () => {
    render(
      <Composer
        value=""
        onChange={() => {}}
        onSend={() => {}}
        onStop={() => {}}
        streaming={false}
        disabledReason={null}
      />,
    )
    expect(document.querySelector('[data-source-control]')).toBeNull()
  })
})

/**
 * The class page owns the selection and hands it back, so the harness mirrors that wiring:
 * the pill reflects whatever the harness currently holds.
 */
function SourceContextHarness({
  documents,
  onPick,
}: {
  documents: DocumentRead[]
  onPick?: (documentId: number | null) => void
}) {
  const [selectedId, setSelectedId] = useState<number | null>(null)
  return (
    <SourceContext
      documents={documents}
      selectedId={selectedId}
      onSelect={(id) => {
        setSelectedId(id)
        onPick?.(id)
      }}
    />
  )
}

describe('the material scope keeps its full behavior behind a compact pill', () => {
  it('opens the picker, scopes to one document, and clears back to all material', async () => {
    const user = userEvent.setup()
    const onPick = vi.fn()
    render(
      <SourceContextHarness
        documents={[
          doc(1, 'lecture_5.pdf'),
          doc(2, 'supplement.md'),
          doc(3, 'scanning_in_progress.pdf', 'parsing'),
        ]}
        onPick={onPick}
      />,
    )

    const chip = screen.getByRole('button', { name: /Choose what Lyra reads/ })
    expect(chip).toHaveTextContent('All material')

    await user.click(chip)
    // The row carries a ready-count note in its accessible name, so match the label.
    const picker = await screen.findByRole('radio', { name: /All material/ })
    expect(picker).not.toBeDisabled()
    // Not-ready material stays visible but not selectable.
    expect(screen.getByRole('radio', { name: /scanning_in_progress/ })).toBeDisabled()

    await user.click(screen.getByRole('radio', { name: 'supplement.md' }))
    expect(onPick).toHaveBeenCalledWith(2)
    // The deliberate scope is visible on the row: the pill names the document...
    await screen.findByRole('button', { name: /Lyra reads only supplement\.md/ })
    // ...and carries its own clear mark.
    const clear = screen.getByRole('button', { name: /Stop reading only supplement\.md/ })
    await user.click(clear)
    expect(onPick).toHaveBeenLastCalledWith(null)
    expect(screen.getByRole('button', { name: /Choose what Lyra reads/ })).toHaveTextContent(
      'All material',
    )
  })

  it('finds a file by search before choosing it', async () => {
    const user = userEvent.setup()
    const onPick = vi.fn()
    render(
      <SourceContextHarness
        documents={[
          doc(1, 'lecture_5.pdf'),
          doc(2, 'supplement.md'),
          doc(3, 'final_exam_review.pdf'),
        ]}
        onPick={onPick}
      />,
    )

    await user.click(screen.getByRole('button', { name: /Choose what Lyra reads/ }))
    const search = await screen.findByLabelText("Search this class's files")
    await user.type(search, 'exam')
    expect(screen.queryByRole('radio', { name: 'lecture_5.pdf' })).toBeNull()
    await user.click(await screen.findByRole('radio', { name: 'final_exam_review.pdf' }))
    expect(onPick).toHaveBeenCalledWith(3)
  })
})

describe('the folder attach keeps its full behavior behind a 24px icon', () => {
  it('attaches from the icon and becomes the compact workspace chip', async () => {
    const user = userEvent.setup()
    const attach = vi.spyOn(api, 'attachAgentWorkspace')
    render(<WorkspaceContextChip />, { wrapper: createWrapper() })

    const icon = await screen.findByRole('button', { name: 'Attach a folder' })
    await user.click(icon)

    await waitFor(() =>
      expect(attach).toHaveBeenCalledWith(CLASS_ID, '/tmp/starter', {
        displayName: undefined,
        readEnabled: true,
      }),
    )
    // The attach lands in place: a compact chip beside the source mark, with its own
    // detach menu - the same row, not a new surface.
    const chip = await screen.findByText('Workspace: starter')
    expect(chip.closest('[data-workspace-chip]')).not.toBeNull()
    expect(screen.queryByRole('button', { name: 'Attach a folder' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Workspace options' })).toBeInTheDocument()
  })

  it('shows the attaching state on the icon itself while the attach is in flight', async () => {
    const user = userEvent.setup()
    let resolveAttach: (workspace: AgentWorkspaceRead) => void
    vi.spyOn(api, 'attachAgentWorkspace').mockImplementation(
      () =>
        new Promise<AgentWorkspaceRead>((resolve) => {
          resolveAttach = resolve
        }),
    )
    render(<WorkspaceContextChip />, { wrapper: createWrapper() })

    const icon = await screen.findByRole('button', { name: 'Attach a folder' })
    await user.click(icon)

    // The icon carries the in-flight state: labeled, and not re-clickable.
    const busy = await screen.findByRole('button', { name: 'Attaching…' })
    expect(busy).toBeDisabled()

    resolveAttach!(workspace())
    await screen.findByText('Workspace: starter')
  })

  it('detaches from the chip and returns to the icon affordance', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'getAgentWorkspace').mockResolvedValue(workspace())
    const detach = vi.spyOn(api, 'detachAgentWorkspace')
    render(<WorkspaceContextChip />, { wrapper: createWrapper() })

    await screen.findByText('Workspace: starter')
    await user.click(screen.getByRole('button', { name: 'Workspace options' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Detach workspace' }))

    await waitFor(() => expect(detach).toHaveBeenCalledWith(CLASS_ID))
    await screen.findByRole('button', { name: 'Attach a folder' })
  })

  it('falls back to the bounded path entry on builds without a picker', async () => {
    vi.spyOn(runtime, 'desktopFolderPickerAvailable').mockReturnValue(false)
    vi.spyOn(runtime, 'pickDesktopWorkspaceDirectory').mockResolvedValue(null)
    const user = userEvent.setup()
    render(
      <>
        <WorkspaceContextChip />
        <PathEntryProbe />
      </>,
      { wrapper: createWrapper() },
    )

    const icon = await screen.findByRole('button', { name: 'Attach a folder' })
    await user.click(icon)

    // No picker on this build: the bounded path entry is where the student names the
    // folder, surfaced by the work surface rather than as setup chrome.
    await waitFor(() => expect(screen.getByTestId('path-entry')).toHaveTextContent('visible'))
  })
})

/** The slice of the work surface that consumes the path-entry state, kept test-local. */
function PathEntryProbe() {
  const { cardPathEntryVisible } = useWorkspaceAttach()
  return <div data-testid="path-entry">{cardPathEntryVisible ? 'visible' : 'hidden'}</div>
}
