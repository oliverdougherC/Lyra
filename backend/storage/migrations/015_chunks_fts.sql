-- Lexical index beside the vectors. The embedder cannot tell two documents apart when
-- they say the same words, and a problem set and its answer key say exactly the same
-- words: the key restates every question verbatim, so cosine distance ranks it among
-- eleven near-identical chunks and it never reaches the served eight. The words being
-- identical is the textbook case for lexical matching, which ranks the verbatim restatement
-- first because it is one. See docs/integration-handoff.md, workstream 1.
--
-- External-content: FTS5 stores only the index and reads text back from `chunks`, so
-- content is never duplicated. Porter stemming so "integrating" matches "integrate";
-- unicode61 handles the math-adjacent punctuation.
create virtual table chunks_fts using fts5(
  content,
  content='chunks',
  content_rowid='id',
  tokenize='porter unicode61'
);

insert into chunks_fts(rowid, content) select id, content from chunks;

create trigger chunks_fts_insert after insert on chunks begin
  insert into chunks_fts(rowid, content) values (new.id, new.content);
end;
create trigger chunks_fts_delete after delete on chunks begin
  insert into chunks_fts(chunks_fts, rowid, content) values ('delete', old.id, old.content);
end;
create trigger chunks_fts_update after update of content on chunks begin
  insert into chunks_fts(chunks_fts, rowid, content) values ('delete', old.id, old.content);
  insert into chunks_fts(rowid, content) values (new.id, new.content);
end;
