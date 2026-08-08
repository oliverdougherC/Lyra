-- The brief: what a draft is, so the writer knows what it is writing. One row per draft,
-- proposed by the assistant (from the title and body, or from an assignment handout it
-- found in the class documents) and confirmed by the student. Every field is prose the
-- student can edit; `source_document_id` records the handout a discerned brief was
-- cross-referenced against, and survives that document's deletion as null rather than
-- taking the brief with it.
create table draft_briefs (
  artifact_id integer primary key references artifacts(id) on delete cascade,
  assignment_type text not null default '',
  summary text not null default '',
  audience text not null default '',
  length_target text not null default '',
  source_document_id integer references documents(id) on delete set null,
  status text not null default 'proposed' check (status in ('proposed', 'confirmed')),
  created_at text not null default (datetime('now')),
  updated_at text not null default (datetime('now'))
);
