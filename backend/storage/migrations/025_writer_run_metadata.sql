-- Honest status for long writer jobs. These columns are null for every other artifact
-- kind and keep the draft status API from guessing job identity from stage prose.
alter table artifacts add column writer_job_kind text
  check (writer_job_kind is null or writer_job_kind in ('pass', 'review'));
alter table artifacts add column writer_job_depth text
  check (writer_job_depth is null or writer_job_depth in ('quick', 'standard', 'deep'));
alter table artifacts add column writer_job_started_at text;
alter table artifacts add column writer_job_completed_at text;
