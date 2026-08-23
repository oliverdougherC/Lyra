-- PLA-295: a durable attempt lifecycle for agent-chat turns.
--
-- One durable user message is one logical turn. Each time the model is run against that
-- turn is an attempt with an explicit state, so a retry reuses the original user message
-- instead of appending a second copy, and the evidence of a failed attempt survives the
-- retry, a reload, and a restart. Tool audit rows carry the attempt that produced them,
-- so retrying after partial tool activity never makes prior records look like the new
-- attempt's work.

create table agent_turn_attempts (
  id integer primary key autoincrement,
  session_id integer not null references chat_sessions(id) on delete cascade,
  -- The one user message this attempt answers. Cascades with the message, so discarding
  -- an empty conversation or deleting a session takes its attempts with it.
  user_message_id integer not null references messages(id) on delete cascade,
  profile text not null,
  -- running: the loop is in flight. completed: an assistant reply committed (and its id is
  -- recorded below). failed: the loop ended without a reply (upstream/timeout/depth/
  -- context/output limit). stopped: the attempt was abandoned by a restart while running.
  state text not null default 'running'
    check (state in ('running', 'completed', 'failed', 'stopped')),
  -- The tool-loop stop reason for a failed/stopped attempt, and a bounded, privacy-safe
  -- sentence explaining it. Null for a running or completed attempt.
  stopped_reason text,
  detail text,
  -- Set only when the attempt completes: the assistant message it committed. A lost HTTP
  -- response is recovered by replaying this rather than running the model again.
  assistant_message_id integer references messages(id) on delete set null,
  created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  finished_at text
);
create index agent_turn_attempts_message_idx on agent_turn_attempts (user_message_id, id);
create index agent_turn_attempts_session_idx on agent_turn_attempts (session_id, id);

-- Every tool audit row an agent turn writes belongs to the attempt that produced it. Null
-- for the pre-existing host-effect rows (apply/execute) and any pre-migration row.
alter table tool_audit_events add column attempt_id integer
  references agent_turn_attempts(id) on delete set null;
create index tool_audit_events_attempt_idx on tool_audit_events (attempt_id);
