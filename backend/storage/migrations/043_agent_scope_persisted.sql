-- PLA-401 final pass: authority for the persisted turn scope.
--
-- Migration 042 made `document_id` a real persisted scope value on agent turns, in which
-- NULL means "All material" - not "no scope was recorded". But a row created before the
-- scope existed cannot be told apart from a modern all-material row by `document_id`
-- alone: in a pre-042 row (and in any row a backend wrote before this flag existed) both
-- mode and document_id are NULL simply because nothing was stored.
--
-- `scope_persisted` is the discriminator: every attempt created by the modern path
-- (agent_attempts.create_attempt) writes 1, which says "this row's mode and document_id
-- are authoritative, null document included". Retry and regenerate read the persisted
-- scope - including an all-material NULL - only from a flagged row, and fall back to
-- request-provided scope (the pre-042 behavior) for unflagged legacy rows.

alter table agent_turn_attempts
  add column scope_persisted integer not null default 0
  check (scope_persisted in (0, 1));
