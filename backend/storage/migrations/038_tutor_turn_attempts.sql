-- PLA-306: a durable attempt lifecycle for tutor-chat turns.
--
-- One durable user message is one logical turn. Each time the model is run against that
-- turn is an attempt with an explicit state, so a retry reuses the original user message
-- instead of appending a second copy, and the evidence of a failed attempt survives the
-- retry, a reload, and a restart.
--
-- Tutor turns have no durable tool effects (no proposals, briefs, or tool audit rows), so
-- there is no targets table and no attempt_id column on tool_audit_events.

create table tutor_turn_attempts (
  id integer primary key autoincrement,
  session_id integer not null references chat_sessions(id) on delete cascade,
  user_message_id integer not null references messages(id) on delete cascade,
  state text not null default 'running'
    check (state in ('running', 'completed', 'failed', 'stopped')),
  stopped_reason text,
  detail text,
  assistant_message_id integer references messages(id) on delete set null,
  created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  finished_at text
);
create index tutor_turn_attempts_message_idx on tutor_turn_attempts (user_message_id, id);
create index tutor_turn_attempts_session_idx on tutor_turn_attempts (session_id, id);
