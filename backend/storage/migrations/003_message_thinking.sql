-- Reasoning models emit their deliberation on a separate channel from the answer. It is
-- stored beside the message rather than inside `content`, so a reopened conversation can
-- still offer the thought without it leaking into the reply the student reads, and so a
-- non-thinking model simply leaves the column empty.
alter table messages add column thinking text not null default '';
