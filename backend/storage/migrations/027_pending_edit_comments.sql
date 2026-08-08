-- Addressing a review finding may produce a pending edit rather than landing directly.
-- Keep that relationship until the student accepts or rejects the proposal; resolving
-- the finding when the model merely proposes prose would claim an edit that never landed.
create table pending_edit_comment_links (
  edit_id integer not null references pending_edits(id) on delete cascade,
  comment_id integer not null references draft_comments(id) on delete cascade,
  created_at text not null default (datetime('now')),
  primary key (edit_id, comment_id)
);

create index idx_pending_edit_comments_comment
  on pending_edit_comment_links(comment_id);
