-- PLA-313: client-generated idempotency key for tutor chat sends.
--
-- A fresh tutor send carries a stable `operation_id` minted once by the
-- browser.  If the connection dies after the server commits the user message
-- but before the HTTP response headers reach the browser, the frontend
-- retries with the same operation_id.  The UNIQUE constraint guarantees at
-- most one durable user-message/attempt per logical send:
--
--   * first arrival   -> insert succeeds, proceed normally.
--   * ambiguous retry -> insert violates the constraint, the handler detects
--     the existing turn and replays/reconciles instead of duplicating.

alter table tutor_turn_attempts add column operation_id text;
create unique index tutor_turn_attempts_op_idx
  on tutor_turn_attempts (session_id, operation_id)
  where operation_id is not null;
