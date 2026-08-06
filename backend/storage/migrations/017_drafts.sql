-- The draft workspace. A draft is an artifact of kind 'draft' (migration 016 widened the
-- checks) with exactly one body part, so revisions, provenance, and step-scoped chat all
-- work unchanged. This migration adds the one table that is genuinely new.
--
-- One pending AI revision per draft body, reviewed hunk by hunk. Base and proposed are
-- full blobs; hunks are DERIVED at read time and never stored. `note` is the user's
-- instruction, carried into the revision note on accept.
create table pending_edits (
  id integer primary key autoincrement,
  part_id integer not null unique references artifact_parts(id) on delete cascade,
  base_content text not null,
  base_hash text not null,
  proposed_content text not null,
  stale integer not null default 0,
  note text,
  created_at text not null default (datetime('now')),
  updated_at text not null default (datetime('now'))
);
