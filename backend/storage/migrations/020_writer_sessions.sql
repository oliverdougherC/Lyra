-- The writer's conversation is a chat session like any other - same transcript, same
-- titles, same deletion - in a third mode. SQLite cannot alter a check constraint, so
-- chat_sessions is rebuilt with the mode check widened to 'writer'. Same dance as
-- migration 016, same reason.
--
-- Messages gain `tool_activity`: the writer's turns run a tool loop, and what it did
-- ("read section 2", "searched the course material") is part of the answer's record,
-- not scaffolding to lose on reload. A JSON array; '[]' for every message that predates
-- the writer, which reads as "did nothing", which is true.

pragma foreign_keys = off;

create table chat_sessions_new (
  id integer primary key autoincrement,
  class_id integer not null references classes(id) on delete cascade,
  title text,
  mode text not null default 'guide' check (mode in ('guide','show','writer')),
  created_at text not null default (datetime('now')),
  artifact_part_id integer references artifact_parts(id) on delete set null
);
insert into chat_sessions_new
  select id, class_id, title, mode, created_at, artifact_part_id
  from chat_sessions;
drop table chat_sessions;
alter table chat_sessions_new rename to chat_sessions;

pragma foreign_keys = on;

alter table messages add column tool_activity text not null default '[]';
