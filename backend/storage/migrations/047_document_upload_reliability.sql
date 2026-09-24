-- Preserve the published index while a refresh is built, retain safe page diagnostics,
-- and bind a logical upload attempt to one committed document.
alter table documents add column refresh_state text;
alter table document_pages add column skip_reason text;
-- Older model-empty replies were called recognized. They were never proof of a
-- genuinely blank page and need the explicit per-page retry path.
update document_pages set state = 'failed', text = null,
  error_message = 'No readable text was found on this page. Try reading it again.'
where state = 'recognized' and length(trim(coalesce(text, ''))) = 0;
update documents set pages_done = (
  select count(*) from document_pages p where p.document_id = documents.id
  and p.state in ('text', 'recognized')
) where exists (select 1 from document_pages p where p.document_id = documents.id);
create table document_index_pages (
  document_id integer not null references documents(id) on delete cascade,
  page_number integer not null,
  primary key (document_id, page_number)
);
insert into document_index_pages (document_id, page_number)
select p.document_id, p.page_number from document_pages p
join documents d on d.id = p.document_id
where d.state = 'ready' and (p.state = 'text' or (p.state = 'recognized' and length(trim(coalesce(p.text, ''))) > 0));
create table upload_operations (
  operation_key text primary key,
  class_id integer not null,
  filename text not null,
  mime text not null,
  sha256 text not null,
  document_id integer references documents(id) on delete set null,
  created_at text not null default (datetime('now'))
);
