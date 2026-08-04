-- What solving and verification need on top of the artifact model.
--
-- Three separate concerns, one migration because they land together in the same build
-- step and none of them is useful without the others. See docs/solver-phase-2.md.

-- The audit trail behind a verdict. Every tool call the verifier made, in the order it
-- made them, with what it asked and what came back.
--
-- A table rather than a blob on the part, because the interface lists these individually
-- and because "3 checks run" has to be a count rather than a parse. It is deliberately
-- not a part: a check is not something the student reads as part of the solution, edits,
-- or asks about, which is what every part kind is.
create table artifact_checks (
  id integer primary key autoincrement,
  part_id integer not null references artifact_parts(id) on delete cascade,
  ordinal integer not null,
  tool text not null,
  -- Stored as the JSON text handed to the tool, not as parsed columns: the arguments
  -- differ per tool and the interface prints them rather than querying them.
  arguments text not null default '{}',
  ok integer not null default 0,
  result text not null default '{}',
  created_at text not null default (datetime('now'))
);
create index idx_checks_part on artifact_checks(part_id);

-- Why a verdict says what it says. A refutation names the check that disagreed and what
-- it returned; an `unchecked` names the reason checking did not run. Held beside the
-- verdict rather than folded into it, because the verdict is an enum a query filters on
-- and this is a sentence a person reads.
alter table artifact_parts add column verdict_detail text;

-- Whether the configured endpoint can run tool calls. Null means nobody has asked yet,
-- which is distinct from asked-and-no: the settings screen renders all three. Reset to
-- null whenever the endpoint or model changes, because the answer belonged to the old one.
alter table settings add column tools_supported integer;
alter table settings add column tools_message text;
