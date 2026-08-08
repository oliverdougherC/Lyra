-- Durable writer intent. A draft has one active plan, while prior plans remain as
-- read-only history so a re-plan never erases the reasoning that produced an earlier
-- draft. Structured fields are JSON text because the project deliberately uses SQLite
-- without an ORM.
create table draft_plans (
  id integer primary key autoincrement,
  artifact_id integer not null references artifacts(id) on delete cascade,
  version integer not null check (version > 0),
  active integer not null default 1 check (active in (0, 1)),
  brief_analysis text not null default '',
  thesis text not null default '',
  argument_map text not null default '[]' check (json_valid(argument_map)),
  created_at text not null default (datetime('now')),
  updated_at text not null default (datetime('now')),
  unique (artifact_id, version)
);

-- SQLite permits many inactive versions but only one active version for a draft.
create unique index idx_draft_plans_one_active
  on draft_plans(artifact_id) where active = 1;
create index idx_draft_plans_history on draft_plans(artifact_id, version desc);

create table draft_plan_sections (
  id integer primary key autoincrement,
  plan_id integer not null references draft_plans(id) on delete cascade,
  section_ref text not null,
  ordinal integer not null check (ordinal >= 0),
  title text not null default '',
  job text not null default '',
  claim text not null default '',
  evidence text not null default '[]' check (json_valid(evidence)),
  source_ids text not null default '[]' check (json_valid(source_ids)),
  word_budget integer not null default 0 check (word_budget >= 0),
  research_notes text not null default '',
  created_at text not null default (datetime('now')),
  updated_at text not null default (datetime('now')),
  unique (plan_id, section_ref),
  unique (plan_id, ordinal)
);

create index idx_draft_plan_sections_order on draft_plan_sections(plan_id, ordinal);
