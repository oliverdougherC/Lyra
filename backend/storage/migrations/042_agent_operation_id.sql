-- PLA-313 (agent): the durable contract for idempotent agent-chat sends.
--
-- The ordinary class conversation now runs every turn on the agent endpoint, so the
-- client-generated idempotency key that tutor chat has had since 039 must apply here
-- with the same semantics:
--
--   * a fresh agent send carries a stable `operation_id` minted once by the browser;
--   * a resend after an ambiguous transport failure carries the same id;
--   * the UNIQUE index guarantees at most one durable user-message/attempt per
--     logical send, so the handler replays a completed reply (or reconciles a
--     failed/stopped one) instead of re-running the tool loop;
--   * a completed operation replays without another model/tool pass;
--   * a reuse with different content, mode, or source scope is refused with a
--     structured `operation_id_mismatch`;
--   * a busy 409 never discards the client's ambiguity key.
--
-- `mode` and `document_id` record the turn context the attempt was asked under. Retry
-- and just-in-time continuation re-run the turn with the scope it was originally asked
-- with rather than rebuilding an unscoped one, and a regenerate that carries no
-- explicit body falls back to the same persisted scope.

alter table agent_turn_attempts add column mode text;
alter table agent_turn_attempts add column document_id integer;
alter table agent_turn_attempts add column operation_id text;
create unique index agent_turn_attempts_op_idx
  on agent_turn_attempts (session_id, operation_id)
  where operation_id is not null;
