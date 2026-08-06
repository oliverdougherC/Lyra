-- Study tools live on the artifact substrate: a flashcard deck is an artifact of cards, a
-- quiz an artifact of questions, and a draft an artifact with one body. SQLite cannot
-- alter a check constraint, so the three artifact tables are rebuilt with their checks
-- widened to the new kinds. Everything else stays byte-identical, including the
-- solver-era column names problems_total/problems_done: they are reused as generic
-- progress counters, and renaming them would ripple through the solver, the API schemas,
-- and the frontend types for zero user-visible gain.
--
-- Foreign keys are managed by hand because the rebuild drops the very tables other
-- tables point at; the keys are restored before the migration ends.

pragma foreign_keys = off;

create table artifacts_new (
  id integer primary key autoincrement,
  class_id integer not null references classes(id) on delete cascade,
  kind text not null check (kind in ('solution_set','flashcard_deck','quiz','draft')),
  title text not null,
  state text not null check (state in
    ('pending','segmenting','awaiting_review','solving','generating','ready','failed','cancelled')),
  stage_detail text,
  problems_total integer,
  problems_done integer not null default 0,
  error_message text,
  created_at text not null default (datetime('now')),
  updated_at text not null default (datetime('now'))
);
insert into artifacts_new
  select id, class_id, kind, title, state, stage_detail, problems_total, problems_done,
         error_message, created_at, updated_at
  from artifacts;
drop table artifacts;
alter table artifacts_new rename to artifacts;
create index idx_artifacts_class on artifacts(class_id);

create table artifact_sources_new (
  artifact_id integer not null references artifacts(id) on delete cascade,
  document_id integer not null references documents(id) on delete cascade,
  role text not null check (role in ('problem_set','reference_solutions','study_source')),
  ordinal integer not null,
  primary key (artifact_id, document_id)
);
insert into artifact_sources_new
  select artifact_id, document_id, role, ordinal
  from artifact_sources;
drop table artifact_sources;
alter table artifact_sources_new rename to artifact_sources;

create table artifact_parts_new (
  id integer primary key autoincrement,
  artifact_id integer not null references artifacts(id) on delete cascade,
  parent_part_id integer references artifact_parts(id) on delete cascade,
  kind text not null check (kind in
    ('problem','step','answer','figure','card','quiz_question','draft_body')),
  ordinal integer not null,
  label text,
  content text not null default '',
  content_type text not null default 'markdown'
    check (content_type in ('markdown','image','json')),
  status text not null default 'pending'
    check (status in ('pending','solving','verifying','complete','failed')),
  origin text not null default 'generated'
    check (origin in ('generated','regenerated','user_corrected')),
  verdict text not null default 'unchecked'
    check (verdict in ('unchecked','verified','refuted','uncheckable')),
  error_message text,
  created_at text not null default (datetime('now')),
  updated_at text not null default (datetime('now')),
  verdict_detail text,
  solve_parts text not null default 'together'
    check (solve_parts in ('together','separately'))
);
insert into artifact_parts_new
  select id, artifact_id, parent_part_id, kind, ordinal, label, content, content_type,
         status, origin, verdict, error_message, created_at, updated_at, verdict_detail,
         solve_parts
  from artifact_parts;
drop table artifact_parts;
alter table artifact_parts_new rename to artifact_parts;
create index idx_parts_artifact on artifact_parts(artifact_id);
create index idx_parts_parent on artifact_parts(parent_part_id);

-- Scheduling state is not part content: it changes on every review, has no revision
-- history, and dies with its card. One row per card part.
create table card_states (
  part_id integer primary key references artifact_parts(id) on delete cascade,
  due_at text not null,
  stability real not null default 0,
  difficulty real not null default 5,
  reps integer not null default 0,
  lapses integer not null default 0,
  state text not null default 'new'
    check (state in ('new','learning','relearning','review')),
  last_review_at text
);

create table card_review_log (
  id integer primary key autoincrement,
  part_id integer not null references artifact_parts(id) on delete cascade,
  rating text not null check (rating in ('again','hard','good','easy')),
  reviewed_at text not null default (datetime('now'))
);
create index idx_review_log_part on card_review_log(part_id);

create table quiz_attempts (
  id integer primary key autoincrement,
  artifact_id integer not null references artifacts(id) on delete cascade,
  started_at text not null default (datetime('now')),
  finished_at text
);
create index idx_attempts_artifact on quiz_attempts(artifact_id);

create table quiz_answers (
  attempt_id integer not null references quiz_attempts(id) on delete cascade,
  part_id integer not null references artifact_parts(id) on delete cascade,
  selected_index integer not null,
  correct integer not null,
  answered_at text not null default (datetime('now')),
  primary key (attempt_id, part_id)
);

pragma foreign_keys = on;
