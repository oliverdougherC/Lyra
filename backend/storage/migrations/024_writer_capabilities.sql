-- Global writer defaults are deliberately conservative. Web access is opt-in and a
-- single serial request remains the reference execution path.
alter table settings add column allow_web_research integer not null default 0
  check (allow_web_research in (0, 1));
alter table settings add column parallel_requests integer not null default 0
  check (parallel_requests in (0, 1));
alter table settings add column parallel_concurrency integer not null default 1
  check (parallel_concurrency >= 1);

-- Nullable values are tri-state overrides: null inherits the global setting, 0 disables
-- it for this class, and 1 enables it. Concurrency also inherits when null.
create table class_writer_capabilities (
  class_id integer primary key references classes(id) on delete cascade,
  allow_web_research integer check (allow_web_research in (0, 1)),
  parallel_requests integer check (parallel_requests in (0, 1)),
  parallel_concurrency integer check (parallel_concurrency is null or parallel_concurrency >= 1),
  updated_at text not null default (datetime('now'))
);
