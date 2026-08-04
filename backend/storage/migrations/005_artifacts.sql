-- The artifact model. Through Phase 1 Lyra held inputs (documents), a derived index
-- (chunks), a transcript (messages), and claims about a class (profile_facts). None of
-- those is a thing Lyra produced that the user keeps, edits, and returns to.
--
-- These tables are deliberately general rather than solver-shaped: the Phase 2 homework
-- solver is their first consumer, and the Phase 4 agent's work product is the same shape.
-- See docs/solver-phase-2.md.

create table artifacts (
  id integer primary key autoincrement,
  class_id integer not null references classes(id) on delete cascade,
  kind text not null check (kind in ('solution_set')),
  title text not null,
  -- Job state lives here rather than in a separate jobs table, matching what document
  -- ingestion actually does. `awaiting_review` is not a transient stage: it is where a
  -- run stops and waits, indefinitely, for the student to confirm the segmentation.
  state text not null check (state in
    ('pending','segmenting','awaiting_review','solving','ready','failed','cancelled')),
  stage_detail text,
  -- Null until segmentation finishes, so "unknown" is distinct from "zero problems".
  problems_total integer,
  problems_done integer not null default 0,
  error_message text,
  created_at text not null default (datetime('now')),
  updated_at text not null default (datetime('now'))
);
create index idx_artifacts_class on artifacts(class_id);

-- Which documents an artifact was produced from, and in what capacity. `role` is what
-- carries the per-run reference-solutions choice, so designating a document as reference
-- material needs no column on `documents` and can differ between two runs.
create table artifact_sources (
  artifact_id integer not null references artifacts(id) on delete cascade,
  document_id integer not null references documents(id) on delete cascade,
  role text not null check (role in ('problem_set','reference_solutions')),
  ordinal integer not null,
  primary key (artifact_id, document_id)
);

-- Parts are a tree. `parent_part_id` is what lets a problem own its steps, and what will
-- let a Phase 3 figure hang off the step that references it without a migration. That is
-- also why `content_type` and the `figure` kind exist now: adding non-text content later
-- would mean rewriting rendering, export, and storage at once.
--
-- Three status columns answering three questions, deliberately not folded into one:
-- `status` is lifecycle, `origin` is who wrote what is currently here, and `verdict` is
-- what checking concluded. One column would produce states no query wants.
create table artifact_parts (
  id integer primary key autoincrement,
  artifact_id integer not null references artifacts(id) on delete cascade,
  parent_part_id integer references artifact_parts(id) on delete cascade,
  kind text not null check (kind in ('problem','step','answer','figure')),
  ordinal integer not null,
  label text,
  content text not null default '',
  content_type text not null default 'markdown'
    check (content_type in ('markdown','image')),
  status text not null default 'pending'
    check (status in ('pending','solving','verifying','complete','failed')),
  origin text not null default 'generated'
    check (origin in ('generated','regenerated','user_corrected')),
  -- `uncheckable` (nothing here could be checked) and `unchecked` (checking did not run)
  -- are both honest non-answers and neither may ever render as a pass.
  verdict text not null default 'unchecked'
    check (verdict in ('unchecked','verified','refuted','uncheckable')),
  error_message text,
  created_at text not null default (datetime('now')),
  updated_at text not null default (datetime('now'))
);
create index idx_parts_artifact on artifact_parts(artifact_id);
create index idx_parts_parent on artifact_parts(parent_part_id);

-- History is per part, because parts are what the user clicks, questions, corrects, and
-- regenerates. `note` carries why a revision exists: the student's correction text, or
-- the verifier's refutation.
create table artifact_part_revisions (
  id integer primary key autoincrement,
  part_id integer not null references artifact_parts(id) on delete cascade,
  revision integer not null,
  content text not null,
  origin text not null check (origin in ('generated','regenerated','user_corrected')),
  note text,
  created_at text not null default (datetime('now'))
);
create index idx_revisions_part on artifact_part_revisions(part_id);
create unique index idx_revisions_part_revision on artifact_part_revisions(part_id, revision);

-- Provenance back to the chunks and pages that informed one part, so a claim can be
-- traced and a citation rendered. Nullable with `on delete set null` on purpose: a
-- student may delete and re-upload a source after a solution exists. Losing the citation
-- is acceptable, losing the solution is not.
create table artifact_provenance (
  id integer primary key autoincrement,
  part_id integer not null references artifact_parts(id) on delete cascade,
  chunk_id integer references chunks(id) on delete set null,
  document_id integer references documents(id) on delete set null,
  page_number integer,
  label text
);
create index idx_provenance_part on artifact_provenance(part_id);
