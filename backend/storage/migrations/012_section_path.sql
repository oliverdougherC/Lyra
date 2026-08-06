-- Where a chunk sits in its document's own structure.
--
-- Through Phase 2 a chunk carried a flat `section_title` guessed by a regex over already
-- flattened text. Measured over a 608-page textbook that regex labelled 595 of 596 chunks
-- and most of the labels were wrong: `Sn`, `I1`, and table-of-contents lines complete with
-- their dot leaders. High coverage of wrong values is worse than none, because a wrong
-- section is a lookup that confidently returns the wrong pages.
--
-- Two columns rather than one, because they answer different questions. `section_path` is
-- the hierarchy, for showing a reader where a claim came from and for telling apart three
-- of the reference book's section titles that appear under two different chapters each.
-- `section_number` is the label the book prints, which is what resolves "use the result
-- from section 4.11" as a lookup rather than as a similarity search.
--
-- Both nullable, and null is not a defect. A syllabus has no outline and never will. A
-- section number is genuinely absent from front matter, an index, and an unnumbered
-- appendix. And every chunk ingested before this migration has null for both: a path is
-- derived from the source file's outline, so there is nothing in the database to backfill
-- it from and only a re-ingest can fill it. Retrieval reads a null path as "this document
-- predates structural parsing" rather than as an error, and the student is offered a
-- re-index rather than having one run on their behalf.
alter table chunks add column section_path text;
alter table chunks add column section_number text;

-- Structural lookup resolves a number to its chunks, scoped to one class, and matches
-- descendants by prefix so asking for section 2.2 also finds 2.2.1. The class column
-- leads because every retrieval is class-scoped.
create index idx_chunks_section_number on chunks(class_id, section_number);
