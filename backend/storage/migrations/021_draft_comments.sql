-- Margin comments on a draft: the reviewer's findings, the writer's replies, and the
-- student's own notes, threaded. The store is deliberately position-dumb (kuhn's model):
-- a thread root keeps the verbatim quoted passage and the char offset it sat at when
-- filed, and every consumer re-resolves the quote against the text it is looking at.
-- Nothing here is a live position, so the student editing above an anchor costs nothing,
-- and a passage that is deleted orphans its comment instead of corrupting it.
--
-- Root-only columns (severity, quote, hint, resolved, orphaned) are null or default on
-- replies, which hang off the root via parent_id and cascade with it.
create table draft_comments (
  id integer primary key,
  part_id integer not null references artifact_parts(id) on delete cascade,
  parent_id integer references draft_comments(id) on delete cascade,
  author text not null check (author in ('reviewer', 'writer', 'student')),
  severity text check (severity in ('critical', 'major', 'minor', 'note')),
  quote text,
  hint integer,
  body text not null,
  resolved integer not null default 0,
  orphaned integer not null default 0,
  created_at text not null default (datetime('now'))
);

create index idx_draft_comments_part on draft_comments(part_id);
