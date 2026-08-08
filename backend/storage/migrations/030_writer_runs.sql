create table writer_runs (
  id integer primary key,
  artifact_id integer not null references artifacts(id) on delete cascade,
  job_kind text not null check (job_kind in ('pass', 'review')),
  depth text not null check (depth in ('quick', 'standard', 'deep')),
  status text not null check (
    status in ('queued', 'running', 'cancel_requested', 'completed', 'failed', 'cancelled')
  ),
  request_json text not null default '{}' check (json_valid(request_json)),
  checkpoint_json text check (checkpoint_json is null or json_valid(checkpoint_json)),
  warnings_json text not null default '[]' check (json_valid(warnings_json)),
  error_message text,
  started_at text,
  cancel_requested_at text,
  finished_at text,
  created_at text not null default (datetime('now')),
  updated_at text not null default (datetime('now'))
);

create index idx_writer_runs_artifact_created on writer_runs(artifact_id, created_at desc, id desc);

create unique index idx_writer_runs_active_artifact
on writer_runs(artifact_id)
where status in ('queued', 'running', 'cancel_requested');
