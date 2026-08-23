-- A monotonic content version on every artifact part: the optimistic-concurrency token
-- that stops a stale draft autosave from silently overwriting newer writing (PLA-289).
--
-- Every body write bumps `content_version` by one. A read hands the client the version it
-- saw; a write states the version it expects and lands only if it still matches. Two
-- writers racing the same body - a slow older autosave, a second browser tab, an AI pass
-- that rewrote a section - therefore resolve into one winner and one deterministic
-- conflict rather than last-writer-wins, and the loser keeps its text to reconcile.
--
-- Applies to every part, not just draft bodies: the counter is cheap, and the same
-- guarantee is worth having wherever `set_part_content` writes. Existing rows start at 0,
-- which is a valid version - the first read/write cycle proceeds from there.

alter table artifact_parts add column content_version integer not null default 0;
