-- Free-response answer grading (PLA-496).
--
-- Fill-blank answers used to arrive as a boolean: the interface compared the typed text
-- against the one stored option and sent 0 on a match, -1 on a miss, so the backend could
-- neither grade a correctly written answer in different notation nor remember what the
-- student actually wrote. The graded submission now carries its raw response and the
-- verdict that layered grading produced, so a reload, a retry, and the attempt history
-- all see the student's words and how they were treated.
--
-- Four forward-only column adds; nothing rebuilds a table and every stored row survives
-- unchanged. Legacy rows keep their stored `correct` value and read back with
-- `verdict` and `response_text` NULL - which is how a pre-grading answer (including a
-- legacy fill-blank miss at selected_index -1, which is NOT a recoverable original
-- response) is told apart from one that went through the layered grader.
--
-- `grading_version` is the version of the grading contract that produced the result.
-- Zero means "never graded by the layered engine"; bumping it in code makes a
-- resubmitted answer re-grade against the new contract instead of replaying the old
-- result.

alter table quiz_answers add column response_text text;
alter table quiz_answers add column verdict text
  check (verdict in ('correct','incorrect','uncertain'));
alter table quiz_answers add column grade_detail text;
alter table quiz_answers add column grading_version integer not null default 0;
