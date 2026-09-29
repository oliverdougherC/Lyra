-- Search cursors use global FTS5 BM25 statistics. A mutation in another class
-- can reorder this class's hits, so keep a transactional index generation.
-- Page-native rows also feed the search result before embeddings are ready.
create table document_search_generation (
  id integer primary key check (id = 1),
  generation integer not null default 0
);
insert into document_search_generation (id, generation) values (1, 0);

create trigger document_search_chunks_insert after insert on chunks begin
  update document_search_generation set generation = generation + 1 where id = 1;
end;
create trigger document_search_chunks_delete after delete on chunks begin
  update document_search_generation set generation = generation + 1 where id = 1;
end;
create trigger document_search_chunks_update after update on chunks begin
  update document_search_generation set generation = generation + 1 where id = 1;
end;

create trigger document_search_pages_insert after insert on document_read_pages begin
  update document_search_generation set generation = generation + 1 where id = 1;
end;
create trigger document_search_pages_delete after delete on document_read_pages begin
  update document_search_generation set generation = generation + 1 where id = 1;
end;
create trigger document_search_pages_update after update on document_read_pages begin
  update document_search_generation set generation = generation + 1 where id = 1;
end;
