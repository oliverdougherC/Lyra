import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import DraftWorkspacePage, {
  DraftConflictDialog,
} from '@/app/classes/[id]/drafts/[artifactId]/page'
import { DraftBodyConflictError } from '@/lib/api'
import { RouterProvider } from '@/router/hooks'
import type { LiveDraftSuggestion } from '@/types'

const controls = vi.hoisted(() => ({
  save: vi.fn(),
  start: vi.fn(),
  state: 'ready',
  wide: false,
  live: null as LiveDraftSuggestion | null,
  draft: {
    id: 7,
    class_id: 1,
    part_id: 8,
    title: 'My essay',
    body: 'Original body',
    body_version: 1,
    state: 'ready',
    stage_detail: null,
    error_message: null,
  },
  comments: [{ id: 5, body: 'Clarify the evidence', resolved: false, section_ref: 'Evidence' }],
}))
vi.mock('@/router/hooks', async (original) => ({
  ...(await original<object>()),
  useParams: () => ({ id: '1', artifactId: '7' }),
}))
vi.mock('@/router/dynamic', () => ({
  default: () =>
    function EditorMock(props: {
      initialMarkdown: string
      onChange: (value: string) => void
      onEditorReady: (view: unknown) => void
    }) {
      return (
        <textarea
          aria-label="Editor"
          defaultValue={props.initialMarkdown}
          onFocus={(e) => props.onEditorReady({ dom: e.currentTarget })}
          onChange={(e) => props.onChange(e.target.value)}
        />
      )
    },
}))
vi.mock('@/components/layout/page-chrome', () => ({
  useFullBleed: vi.fn(),
  useImmersiveChrome: vi.fn(),
  HeaderCrumb: () => null,
}))
vi.mock('@/lib/hooks/use-media-query', () => ({
  useMediaQuery: (query: string) => query === '(min-width: 1280px)' && controls.wide,
}))
vi.mock('@/lib/hooks/use-classes', () => ({
  useClasses: () => ({ data: [{ id: 1, name: 'History' }] }),
}))
vi.mock('@/lib/hooks/use-drafts', async (original) => ({
  ...(await original<object>()),
  useDraft: () => ({ data: controls.draft }),
  useDraftStatus: () => ({ data: { state: controls.state } }),
  usePendingEdit: () => ({ data: null }),
  useComments: () => ({ data: controls.comments }),
  useWriterSessions: () => ({ data: [] }),
  useExportAvailability: () => ({ data: { available: true } }),
  useLiveDraftSuggestion: () => ({ data: controls.live }),
  useStartPass: () => ({ mutateAsync: controls.start, isPending: false }),
  useUpdateBody: () => ({ mutateAsync: controls.save, isPending: false }),
}))
vi.mock('@/components/drafts/brief-card', () => ({ BriefCard: () => null }))
vi.mock('@/components/drafts/source-ledger', () => ({ SourceLedger: () => <p>Source list</p> }))
vi.mock('@/components/drafts/plan-panel', async () => {
  const { useState } = await import('react')
  return {
    PlanPanel: () => {
      const [text, setText] = useState('Original plan')
      return (
        <textarea aria-label="Plan thesis" value={text} onChange={(e) => setText(e.target.value)} />
      )
    },
  }
})
vi.mock('@/components/drafts/comment-list', () => ({
  CommentList: (props: {
    onAddressComment: (comment: unknown) => Promise<void>
    addressingDisabled: boolean
  }) => (
    <button
      disabled={props.addressingDisabled}
      onClick={() => void props.onAddressComment(controls.comments[0])}
    >
      Address comment
    </button>
  ),
}))
vi.mock('@/components/chat/chat-pane', () => ({ ChatPane: () => <p>Assistant composer</p> }))
vi.mock('@/components/solutions/revision-history', () => ({
  RevisionHistory: ({ part }: { part: unknown }) => (part ? <p>History opened</p> : null),
}))

function renderWorkspace() {
  window.history.replaceState({}, '', '/#/classes/1/drafts/7')
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const contents = () => (
    <QueryClientProvider client={client}>
      <RouterProvider>
        <DraftWorkspacePage />
      </RouterProvider>
    </QueryClientProvider>
  )
  const view = render(contents())
  return { ...view, refresh: () => view.rerender(contents()) }
}
async function openTool(name: string) {
  await userEvent.click(screen.getByRole('combobox', { name: 'Draft tool' }))
  await userEvent.click(screen.getByRole('option', { name }))
}
beforeEach(() => {
  controls.save.mockReset().mockResolvedValue({ version: 2 })
  controls.start.mockReset().mockResolvedValue({})
  controls.state = 'ready'
  controls.live = null
  controls.wide = false
  window.localStorage.clear()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.setPointerCapture = vi.fn()
  Element.prototype.releasePointerCapture = vi.fn()
  Element.prototype.scrollIntoView = vi.fn()
})

describe('writer route coordination', () => {
  it('preserves unsaved live paragraphs when View plan opens another tool', async () => {
    controls.live = {
      id: 14,
      artifact_id: 7,
      run_id: 22,
      status: 'ready',
      stage: 'transitions',
      stage_detail: 'Ready to review',
      version: 4,
      base_content: 'Original body',
      blocks: [
        {
          id: 101,
          block_key: 'intro',
          section_ref: '1',
          ordinal: 1,
          kind: 'paragraph',
          heading: 'Introduction',
          content: 'Original introduction.',
          status: 'drafted',
          target_words: 180,
          summary: null,
          revision: 2,
          user_revision: 0,
        },
      ],
    }
    const view = renderWorkspace()
    const paragraph = await screen.findByRole('textbox', { name: 'Introduction draft block' })
    await userEvent.clear(paragraph)
    await userEvent.type(paragraph, 'My unsaved paragraph')
    controls.wide = true
    view.refresh()
    expect(screen.getByRole('textbox', { name: 'Introduction draft block' })).toBe(paragraph)
    expect(paragraph).toHaveValue('My unsaved paragraph')
    controls.wide = false
    view.refresh()
    expect(screen.getByRole('textbox', { name: 'Introduction draft block' })).toBe(paragraph)
    await userEvent.click(screen.getByRole('button', { name: 'View plan' }))
    expect(screen.getByRole('textbox', { name: 'Plan thesis' })).toBeVisible()
    await openTool('Draft · Live draft')
    expect(screen.getByRole('textbox', { name: 'Introduction draft block' })).toHaveValue(
      'My unsaved paragraph',
    )
    expect(screen.getAllByText('Unsaved edits')[0]).toBeVisible()
    await openTool('Assistant')
    await openTool('Draft · Live draft')
    expect(screen.getByRole('textbox', { name: 'Introduction draft block' })).toHaveValue(
      'My unsaved paragraph',
    )
  })

  it('keeps plan edits across resizing and visiting sources and opens history directly', async () => {
    const view = renderWorkspace()
    await openTool('Plan')
    const thesis = screen.getByRole('textbox', { name: 'Plan thesis' })
    await userEvent.clear(thesis)
    await userEvent.type(thesis, 'Unsaved thesis')
    const editor = screen.getByRole('textbox', { name: 'Editor' })
    controls.wide = true
    view.refresh()
    expect(screen.getByRole('textbox', { name: 'Editor' })).toBe(editor)
    expect(screen.getByRole('textbox', { name: 'Plan thesis' })).toBe(thesis)
    expect(thesis).toHaveValue('Unsaved thesis')
    controls.wide = false
    view.refresh()
    await openTool('Sources')
    expect(screen.getByText('Source list')).toBeVisible()
    await openTool('Plan')
    expect(screen.getByRole('textbox', { name: 'Plan thesis' })).toHaveValue('Unsaved thesis')
    await openTool('History')
    expect(screen.getByText('History opened')).toBeInTheDocument()
  })

  it('waits for the current body save before addressing a comment and blocks competing launches', async () => {
    let finishSave!: (value: { version: number }) => void
    controls.save.mockImplementation(
      () =>
        new Promise((resolve) => {
          finishSave = resolve
        }),
    )
    renderWorkspace()
    await userEvent.type(screen.getByRole('textbox', { name: 'Editor' }), ' revised')
    await openTool('Review · Comments (1)')
    await userEvent.click(screen.getByRole('button', { name: 'Address comment' }))
    expect(controls.save).toHaveBeenCalledWith({
      content: 'Original body revised',
      expected_version: 1,
    })
    expect(controls.start).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Address comment' })).toBeDisabled()
    await act(async () => finishSave({ version: 2 }))
    await waitFor(() =>
      expect(controls.start).toHaveBeenCalledWith({
        instruction: 'Address this review finding: Clarify the evidence',
        sections: ['Evidence'],
        address_comment_id: 5,
        depth: 'standard',
      }),
    )
  })

  it.each(['failure', 'conflict'])(
    'does not address a comment after a save %s',
    async (outcome) => {
      controls.save.mockRejectedValue(
        outcome === 'failure'
          ? new Error('offline')
          : new DraftBodyConflictError(409, {
              detail: 'Changed',
              code: 'stale_body_version',
              current_version: 9,
              server_body: 'Remote body',
            }),
      )
      renderWorkspace()
      await userEvent.type(screen.getByRole('textbox', { name: 'Editor' }), ' revised')
      await openTool('Review · Comments (1)')
      await userEvent.click(screen.getByRole('button', { name: 'Address comment' }))
      await waitFor(() => expect(controls.save).toHaveBeenCalled())
      expect(controls.start).not.toHaveBeenCalled()
      if (outcome === 'conflict')
        expect(await screen.findByRole('dialog')).toHaveTextContent('Original body revised')
    },
  )

  it('disables Address while a background writing job is running', async () => {
    controls.state = 'generating'
    renderWorkspace()
    await openTool('Review · Comments (1)')
    expect(screen.getByRole('button', { name: 'Address comment' })).toBeDisabled()
  })
})

describe('conflict preservation', () => {
  it('shows and copies both versions before any replacement', async () => {
    const user = userEvent.setup()
    const copy = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue()
    const keepMine = vi.fn()
    const useServer = vi.fn()
    render(
      <DraftConflictDialog
        localBody="My unsaved paragraph"
        conflict={{ serverBody: 'Remote paragraph', serverVersion: 3 }}
        onKeepMine={keepMine}
        onUseServer={useServer}
      />,
    )
    expect(screen.getByRole('region', { name: 'Your unsaved writing' })).toHaveTextContent(
      'My unsaved paragraph',
    )
    expect(screen.getByRole('region', { name: 'The version saved elsewhere' })).toHaveTextContent(
      'Remote paragraph',
    )
    await user.click(screen.getByRole('button', { name: 'Copy your unsaved writing' }))
    await user.click(screen.getByRole('button', { name: 'Copy the version saved elsewhere' }))
    expect(copy.mock.calls).toEqual([['My unsaved paragraph'], ['Remote paragraph']])
    expect(screen.getByRole('button', { name: 'Download both versions' })).toBeEnabled()
    expect(keepMine).not.toHaveBeenCalled()
    expect(useServer).not.toHaveBeenCalled()
  })
})
