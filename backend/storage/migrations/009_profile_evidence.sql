-- A profile fact becomes a claim about the class, and documents become evidence for it.
--
-- Until now a fact was a per-document extraction row, so a class with sixteen uploads held
-- sixteen copies of its course code and a dozen of every topic its assignments touched. The
-- profile was unreadable and it spent the system-prompt budget restating itself. Evidence
-- moves into its own table so the same claim can be attested by many documents while staying
-- one row, and so the count of attesting documents is available as the relevance signal it is.

create table profile_fact_sources (
  fact_id integer not null references profile_facts(id) on delete cascade,
  document_id integer not null references documents(id) on delete cascade,
  primary key (fact_id, document_id)
) without rowid;
create index idx_fact_sources_document on profile_fact_sources(document_id);

-- Every existing fact was attested by exactly the one document that proposed it.
insert into profile_fact_sources (fact_id, document_id)
select id, source_document_id from profile_facts where source_document_id is not null;

-- Fold the duplicates already on disk onto their earliest row. Only exact matches are folded
-- here: the normalization that catches `Time-Invariance` against `Time Invariance` lives in
-- Python and cannot be reproduced in SQL, and a migration that guessed would merge rows the
-- runtime rule would have kept apart. The remaining near-duplicates are what the consolidation
-- pass is for, and it runs over this class the next time anything is uploaded to it.
create temporary table profile_fact_merge as
select f.id as loser_id, min(g.id) as winner_id
from profile_facts f
join profile_facts g
  on g.class_id is f.class_id and g.kind = f.kind and g.label = f.label and g.value = f.value
group by f.id;

insert or ignore into profile_fact_sources (fact_id, document_id)
select m.winner_id, s.document_id
from profile_fact_merge m
join profile_fact_sources s on s.fact_id = m.loser_id;

-- A decision the user made on any copy survives the fold. Rejection is applied first and
-- confirmation guards on it, so a winner cannot come out of this both confirmed and rejected.
update profile_facts set rejected = 1, confirmed = 0
where id in (
  select m.winner_id from profile_fact_merge m
  join profile_facts l on l.id = m.loser_id
  where l.rejected = 1
);
update profile_facts set confirmed = 1
where rejected = 0 and id in (
  select m.winner_id from profile_fact_merge m
  join profile_facts l on l.id = m.loser_id
  where l.confirmed = 1
);

delete from profile_facts
where id in (select loser_id from profile_fact_merge where loser_id <> winner_id);

drop table profile_fact_merge;

-- The two marks that make a fact untouchable by the consolidation pass. `edited` is separate
-- from `confirmed` because correcting a value deliberately does not confirm the fact, and a
-- correction the user typed must still survive a merge that would have deleted the row.
alter table profile_facts add column edited integer not null default 0;

-- Whether the consolidation pass has already considered this fact. A re-upload that proposes
-- nothing new leaves every fact consolidated, and the pass then costs no model call at all.
alter table profile_facts add column consolidated integer not null default 0;
