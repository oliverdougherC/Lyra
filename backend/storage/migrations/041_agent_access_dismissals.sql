-- PLA-401: a bounded "Not now" for just-in-time workspace access requests.
--
-- When a student defers an access request card, the deferral is recorded against the
-- conversation that asked: it keeps the card from nagging again across reloads and
-- unmounts, and it tells the model the student has already answered once, so the model
-- proceeds instead of asking again. It is deliberately neither permanent nor a
-- permission decision: nothing is granted or revoked, the record expires after a
-- bounded window (see agent_store.ACCESS_DISMISSAL_TTL_SECONDS), and it dies with the
-- conversation.

create table agent_access_dismissals (
  class_id integer not null references classes(id) on delete cascade,
  session_id integer not null references chat_sessions(id) on delete cascade,
  scope text not null check (scope in ('attach', 'read', 'propose_changes', 'run_commands')),
  dismissed_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  primary key (class_id, session_id, scope)
);
create index agent_access_dismissals_session_idx on agent_access_dismissals (session_id);
