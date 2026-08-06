-- Per-page state, so a page can succeed, fail, and be retried on its own.
--
-- `documents.pages_done` was written exactly once, at the end of a run, and that was honest
-- while every page of a document shared one outcome: either the file parsed or it did not.
-- Recognition breaks it. A page now costs seconds of model time, can fail by itself, and is
-- worth retrying by itself, which makes it a row rather than a column. A document whose
-- page 7 could not be read is a document with thirty-nine good pages and one retry.
--
-- `text` holds a transcription and nothing else. A page with a text layer re-extracts from
-- the file in well under a millisecond, so storing it here would only be a second copy that
-- can go stale. A transcribed page cost seconds of inference, so it is kept and spliced
-- back in on every later parse: re-indexing a document must never re-run recognition.
create table document_pages (
  document_id integer not null references documents(id) on delete cascade,
  page_number integer not null,
  state text not null check (state in ('text', 'scanned', 'recognized', 'failed')),
  text text,
  error_message text,
  primary key (document_id, page_number)
);

-- Recognition is opt-in per document, and the opt-in belongs on the row rather than on the
-- queue item. The queue lives in memory, so a restart mid-run would otherwise drop the
-- request on the floor, and `reconcile_interrupted` would requeue the document to be read
-- without recognition and land it looking finished.
alter table documents add column recognize integer not null default 0;

-- Whether the configured endpoint can see, in the same three states as `tools_supported`:
-- null for nobody has asked, 0 for asked and no, 1 for yes. An endpoint that cannot read an
-- image is an ordinary configuration rather than an error, and recognition has to say so
-- plainly rather than offering an action that will fail one page at a time.
alter table settings add column vision_supported integer;
alter table settings add column vision_message text;
