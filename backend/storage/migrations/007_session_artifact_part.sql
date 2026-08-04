-- Clicking a step of a solution opens a Guide exchange about that step. It reuses the
-- chat stack entirely: no second message store, no parallel prompt builder, no new
-- streaming protocol. All it needs is for a session to remember what it is anchored to.
--
-- `on delete set null` rather than cascade: deleting the solution should not delete the
-- conversation the student had about it. The exchange stands on its own once it exists,
-- and losing the anchor is a smaller loss than losing the transcript.
alter table chat_sessions add column artifact_part_id integer references artifact_parts(id)
  on delete set null;
