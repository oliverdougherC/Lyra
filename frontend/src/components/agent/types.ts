export type AgentGrantKey = 'web' | 'workspace_read' | 'change_proposals' | 'commands'

export type AgentGrantState = {
  key: AgentGrantKey
  label: string
  description: string
  enabled: boolean
  inherited?: boolean
  unavailable?: boolean
  revoked?: boolean
  blockedReason?: string
  note?: string
  pendingInvalidationCount?: number
}

export type WorkspaceAttachment = {
  label: string
  rootPath: string
  helperText?: string
}

export type AgentSourceReference = {
  id: string | number
  kind: 'course' | 'web'
  title: string
  detail?: string
  href?: string
  truncated?: boolean
}

export type AgentProposalReference = {
  id: string | number
  kind: 'profile_fact' | 'workspace_change' | 'command_request' | 'draft_comment'
  title: string
  state: 'pending' | 'partially_applied' | 'applied' | 'rejected' | 'stale' | 'failed' | 'confirmed'
}

export type AgentToolActivity = {
  id: string | number
  title: string
  toolLabel: string
  summary?: string
  targetLabel?: string
  timestampLabel?: string
  status: 'running' | 'completed' | 'failed' | 'disabled' | 'unavailable'
  failureReason?: string
  disabledReason?: string
  truncated?: boolean
  source?: AgentSourceReference
  proposal?: AgentProposalReference
}

export type WorkspaceChangeHunk = {
  hash: string
  index: number
  lines: string[]
  header?: string
  decision?: 'accepted' | 'rejected'
}

export type WorkspaceChangeReview = {
  id: string | number
  path: string
  rationale?: string
  state: 'pending' | 'partially_applied' | 'applied' | 'rejected' | 'stale' | 'failed'
  summary?: string
  currentContent?: string
  proposedContent?: string
  hunks: WorkspaceChangeHunk[]
}

export type AgentCommandRequest = {
  id: string | number
  argv: string[]
  cwd: string
  reason: string
  expectedSignal?: string
  timeoutSeconds: number
  networkRisk?: 'none' | 'possible' | 'unknown'
  state: 'pending' | 'running' | 'completed' | 'failed' | 'timed_out' | 'rejected' | 'abandoned'
  unavailableReason?: string
  stdout?: string
  stderr?: string
  exitCode?: number | null
  truncated?: boolean
  confirmedAtLabel?: string
}
