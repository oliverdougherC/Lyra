-- Study reliability tranche (PLA-169, PLA-277, PLA-296). Three independent additions,
-- all forward-only column/table/index adds so no table is rebuilt and existing student
-- data is preserved untouched. Foreign keys stay on throughout: nothing here drops a
-- table other rows point at.

-- PLA-169: durable study-generation intent. A queued deck or quiz kept its options only
-- inside the in-memory worker job, so a restart could not reconstruct queued work and
-- failed even artifacts that had not begun. One row per study artifact records exactly
-- what the job needs to run again: the same fields the in-memory job carries. The row is
-- written before the job is enqueued and cascades away with its artifact.
create table study_jobs (
  artifact_id integer primary key references artifacts(id) on delete cascade,
  kind text not null check (kind in ('flashcard_deck','quiz')),
  cards_per_topic integer not null,
  count integer not null,
  difficulty text not null,
  -- The quiz question types as a JSON array of strings. Unused for a deck, but stored so
  -- one shape reconstructs either kind.
  types text not null,
  -- The exact accepted source document ids as a JSON array, in reading order. This is the
  -- consistent snapshot the worker revalidates against (PLA-291): a source that is deleted
  -- or leaves `ready` after the request must fail generation visibly rather than let it
  -- run against a quietly smaller or different set than the student chose.
  source_ids text not null,
  created_at text not null default (datetime('now'))
);

-- PLA-296: make flashcard review idempotent and concurrency-safe. A client-generated
-- operation id de-duplicates a retried review; the scheduling state the review produced
-- is stored beside it so a duplicate returns the original result unchanged rather than
-- recomputing from whatever the card looks like now. op_id is nullable so the rows the
-- deck already logged (which had no operation id) survive; the unique index ignores them.
alter table card_review_log add column op_id text;
alter table card_review_log add column result_state text;
create unique index idx_review_log_op on card_review_log(op_id) where op_id is not null;

-- PLA-277: durable, resumable, causally correct quiz attempts.
--   question_count    - the quiz's real question count when the attempt began, the honest
--                       denominator for a score no matter how many answers were recorded.
--   question_part_ids - a JSON snapshot of the attempt's fixed question set and order, so
--                       an answer can never attach to a different question set after the
--                       quiz is edited or regenerated.
--   result            - the JSON scored result, written once at finish, so a lost finish
--                       response can be retried and return the same score without
--                       double-counting.
--   abandoned         - an attempt the student explicitly restarted away from: finished
--                       (so it frees the one-active slot) but not a real completion.
alter table quiz_attempts add column question_count integer;
alter table quiz_attempts add column question_part_ids text;
alter table quiz_attempts add column result text;
alter table quiz_attempts add column abandoned integer not null default 0;

-- The pre-035 schema let each start create another attempt, so an upgrading database may
-- already hold several active attempts for one quiz. Collapse them to the newest before
-- the one-active-attempt invariant is enforced: the rest are marked abandoned (retained,
-- never deleted) so no attempt's answers are lost.
update quiz_attempts set finished_at = datetime('now'), abandoned = 1
where finished_at is null
  and id not in (
    select max(id) from quiz_attempts where finished_at is null group by artifact_id
  );

-- One active (unfinished) attempt per quiz. An abandoned attempt has finished_at set, so
-- it does not occupy the slot.
create unique index idx_attempts_one_active on quiz_attempts(artifact_id)
  where finished_at is null;
