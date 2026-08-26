-- PLA-310: a durable attempt lifecycle for writer-chat turns.
--
-- Mirrors the agent-turn attempts table (migration 036), scoped to writer chat.
-- One durable user message is one logical writer turn. Each run of the model
-- against that turn is an attempt with an explicit state, so a retry reuses the
-- original user message instead of appending a duplicate, and the evidence of a
-- failed attempt survives the retry, a reload, and a restart.

create table writer_turn_attempts (
  id integer primary key autoincrement,
  session_id integer not null references chat_sessions(id) on delete cascade,
  user_message_id integer not null references messages(id) on delete cascade,
  intent text not null,
  state text not null default 'planned'
    check (state in ('planned', 'running', 'completed', 'failed', 'stopped')),
  stopped_reason text,
  detail text,
  assistant_message_id integer references messages(id) on delete set null,
  created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  finished_at text
);
create index writer_turn_attempts_message_idx on writer_turn_attempts (user_message_id, id);
create index writer_turn_attempts_session_idx on writer_turn_attempts (session_id, id);

-- Ownership ledger for durable writer targets (proposals, briefs, comments).
-- INSERT OR IGNORE preserves the original producer on idempotent retries.
create table writer_attempt_targets (
  attempt_id integer not null references writer_turn_attempts(id) on delete cascade,
  target_kind text not null,
  target_id integer not null,
  created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  primary key (target_kind, target_id)
);
create index writer_attempt_targets_attempt_idx on writer_attempt_targets (attempt_id);
