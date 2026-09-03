-- PLA-313 (agent): client-generated idempotency key for agent-chat sends.
--
-- Mirrors 039_chat_operation_id.sql for the agent's non-streaming turns. A fresh
-- agent send carries a stable operation_id minted once by the browser. If the
-- connection dies after the server commits the user message + attempt but before
-- the HTTP response reaches the browser, the frontend resends with the same
-- operation_id. The UNIQUE constraint guarantees at most one durable
-- user-message/attempt per logical send, so the handler replays a completed
-- reply (or reconciles a failed/stopped one) instead of re-running the tool loop.

alter table agent_turn_attempts add column operation_id text;
create unique index agent_turn_attempts_op_idx
  on agent_turn_attempts (session_id, operation_id)
  where operation_id is not null;
