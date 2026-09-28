-- Page-native text is available before embeddings and keeps physical-page citations.
-- Refresh replaces this evidence only with a validated new index. Legacy rows are
-- read from their immutable source file until they are next refreshed.
create table document_read_pages (
  document_id integer not null references documents(id) on delete cascade,
  page_number integer not null,
  generation text not null,
  content text not null,
  primary key (document_id, page_number)
);
