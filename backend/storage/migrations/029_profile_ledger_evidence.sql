-- Web-researched profile facts remain proposals, but retain the exact immutable ledger
-- evidence that caused the proposal. They stay low-confidence and inactive until the
-- student confirms them through the existing profile review surface.

alter table profile_facts add column source_writer_id integer
  references writer_sources(id) on delete set null;
alter table profile_facts add column source_excerpt_id integer
  references writer_source_excerpts(id) on delete set null;
create index idx_profile_facts_writer_source on profile_facts(source_writer_id);
