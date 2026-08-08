-- Phase 4: default-off agent capabilities, durable proposals/audit, and immutable web
-- snapshot revisions. The model can create proposals only; user-only endpoints consume
-- bound confirmation nonces before any host effect.

alter table settings add column firecrawl_base_url text not null
  default 'http://127.0.0.1:3002';
alter table settings add column firecrawl_scrape_enabled integer not null default 0
  check (firecrawl_scrape_enabled in (0, 1));

create table class_workspaces (
  id integer primary key autoincrement,
  class_id integer not null unique references classes(id) on delete cascade,
  root_path text not null,
  display_name text not null,
  root_device integer not null,
  root_inode integer not null,
  read_enabled integer not null default 0 check (read_enabled in (0, 1)),
  change_proposals_enabled integer not null default 0
    check (change_proposals_enabled in (0, 1)),
  commands_enabled integer not null default 0 check (commands_enabled in (0, 1)),
  created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

create table workspace_changes (
  id integer primary key autoincrement,
  workspace_id integer not null references class_workspaces(id) on delete cascade,
  session_id integer not null references chat_sessions(id) on delete cascade,
  relative_path text not null,
  base_hash text not null,
  base_content text not null,
  proposed_content text not null,
  file_device integer not null,
  file_inode integer not null,
  file_mode integer not null,
  newline text check (newline is null or newline in (char(10), char(13), char(13) || char(10))),
  rationale text,
  state text not null default 'pending' check (state in
    ('pending','partially_applied','applied','rejected','stale','failed')),
  accepted_hunks_json text not null default '[]',
  rejected_hunks_json text not null default '[]',
  before_hash text not null,
  after_hash text,
  state_reason text,
  created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
create index workspace_changes_workspace_state_idx
  on workspace_changes (workspace_id, state);
create index workspace_changes_session_idx on workspace_changes (session_id);

create table command_requests (
  id integer primary key autoincrement,
  workspace_id integer not null references class_workspaces(id) on delete cascade,
  session_id integer not null references chat_sessions(id) on delete cascade,
  argv_json text not null,
  relative_cwd text not null,
  reason text not null,
  expected_signal text,
  timeout_seconds integer not null check (timeout_seconds between 1 and 600),
  state text not null default 'pending' check (state in
    ('pending','running','completed','failed','timed_out','rejected','abandoned')),
  confirmed_at text,
  started_at text,
  finished_at text,
  exit_code integer,
  stdout_text text,
  stderr_text text,
  state_reason text,
  created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
create index command_requests_workspace_state_idx
  on command_requests (workspace_id, state);
create index command_requests_session_idx on command_requests (session_id);
create unique index command_requests_one_active_per_workspace
  on command_requests (workspace_id) where state = 'running';

create table confirmation_nonces (
  id text primary key,
  token_hash text not null unique,
  origin text not null,
  class_id integer references classes(id) on delete cascade,
  session_id integer references chat_sessions(id) on delete cascade,
  action_kind text not null check (action_kind in ('apply_change', 'execute_command')),
  target_id text not null,
  current_hash text,
  payload_hash text not null,
  expires_at text not null,
  consumed_at text,
  created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
create index confirmation_nonces_expires_idx on confirmation_nonces (expires_at);

create table tool_audit_events (
  id text primary key,
  caller_kind text not null,
  caller_id text,
  class_id integer references classes(id) on delete set null,
  session_id integer references chat_sessions(id) on delete set null,
  artifact_id integer references artifacts(id) on delete set null,
  tool text not null,
  capability text not null,
  effect text not null,
  arguments_json text not null,
  target_kind text,
  target_id text,
  policy_decision text not null,
  state text not null check (state in
    ('started','succeeded','refused','failed','timed_out','abandoned','stale','rejected')),
  result_summary_json text,
  error_message text,
  abandonment_reason text,
  started_at text not null,
  finished_at text,
  updated_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
create index tool_audit_events_state_idx on tool_audit_events (state);
create index tool_audit_events_session_idx on tool_audit_events (session_id, started_at);

-- The writer_sources row remains the stable logical citation target. Each fetched web
-- representation is append-only here, and excerpts bind to the revision they came from.
create table writer_source_revisions (
  id integer primary key autoincrement,
  source_id integer not null references writer_sources(id) on delete cascade,
  revision integer not null check (revision >= 1),
  final_url text not null,
  content_type text,
  snapshot text not null,
  snapshot_hash text,
  truncated integer not null default 0 check (truncated in (0, 1)),
  accessed_at text not null,
  created_at text not null default (datetime('now')),
  unique (source_id, revision)
);
create index writer_source_revisions_source_idx
  on writer_source_revisions (source_id, revision desc);

insert into writer_source_revisions
  (source_id, revision, final_url, content_type, snapshot, snapshot_hash, truncated, accessed_at)
select id, 1, url, null, snapshot, null, 0, accessed_at
from writer_sources
where source_type = 'web';

alter table writer_sources add column current_revision_id integer
  references writer_source_revisions(id) on delete set null;
update writer_sources
set current_revision_id = (
  select r.id from writer_source_revisions r
  where r.source_id = writer_sources.id
  order by r.revision desc limit 1
)
where source_type = 'web';

alter table writer_source_excerpts add column source_revision_id integer
  references writer_source_revisions(id) on delete set null;
update writer_source_excerpts
set source_revision_id = (
  select current_revision_id from writer_sources s where s.id = writer_source_excerpts.source_id
)
where source_revision_id is null;
