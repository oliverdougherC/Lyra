create table live_draft_suggestions (
  id integer primary key autoincrement,
  artifact_id integer not null references artifacts(id) on delete cascade,
  run_id integer not null,
  stage text not null default '',
  status text not null default 'pending',
  detail text,
  version integer not null default 1,
  base_content text not null,
  base_hash text not null,
  created_at text not null default (datetime('now')),
  updated_at text not null default (datetime('now')),
  unique (artifact_id, run_id)
);

create index idx_live_draft_suggestions_artifact_latest
  on live_draft_suggestions (artifact_id, id desc);

create table live_draft_blocks (
  id integer primary key autoincrement,
  suggestion_id integer not null references live_draft_suggestions(id) on delete cascade,
  stable_key text not null,
  section_ref text,
  paragraph_ordinal integer not null default 0,
  kind text not null default 'paragraph',
  heading text,
  content text not null default '',
  status text not null default 'pending',
  target_words integer,
  summary text,
  context_json text not null default '{}' check (json_valid(context_json)),
  metadata_json text not null default '{}' check (json_valid(metadata_json)),
  revision integer not null default 0,
  user_revision integer not null default 0,
  created_at text not null default (datetime('now')),
  updated_at text not null default (datetime('now')),
  unique (suggestion_id, stable_key)
);

create index idx_live_draft_blocks_suggestion_ordinal
  on live_draft_blocks (suggestion_id, paragraph_ordinal, id);
