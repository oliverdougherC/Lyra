import { stageLabel, type ProcessingStage } from '@/components/chat/thinking-indicator'
import type { WriterActivity } from '@/types'

// Activity frames describe completed calls, not tools still running. Keep the headline
// bounded and independent of model text, tool arguments, and the answer itself.
const COMPLETED_TOOL_LABELS: Record<string, string> = {
  read_brief: 'Read the brief',
  read_outline: 'Read the outline',
  read_plan: 'Read the writing plan',
  read_section: 'Read a section',
  search_course_material: 'Searched course material',
  search_web: 'Searched the web',
  fetch_source: 'Fetched a source',
  record_source_excerpt: 'Recorded cited evidence',
  list_class_documents: 'Listed class documents',
  read_comments: 'Read the comments',
  reply_to_comment: 'Replied to a comment',
  add_comment: 'Added a comment',
  save_brief: 'Proposed a brief',
  start_draft_pass: 'Started a draft pass',
  start_review: 'Started a review',
  propose_revision: 'Proposed a revision',
  write_section: 'Wrote a section',
}

export function activityLabel(
  entries: WriterActivity[],
  stage: ProcessingStage | null = null,
): string {
  const latest = entries.at(-1)
  if (!latest) return stageLabel(stage)
  if (!latest.ok) return 'Thinking'
  return Object.hasOwn(COMPLETED_TOOL_LABELS, latest.tool)
    ? COMPLETED_TOOL_LABELS[latest.tool]
    : 'Thinking'
}
