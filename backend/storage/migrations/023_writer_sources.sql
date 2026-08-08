-- One source mechanism for course readings and web research. Web pages are snapshotted
-- at research time; course documents keep their document link while it exists and their
-- snapshot if the upload is later removed.
create table writer_sources (
  id integer primary key autoincrement,
  class_id integer not null references classes(id) on delete cascade,
  source_type text not null check (source_type in ('course', 'web')),
  document_id integer references documents(id) on delete set null,
  url text,
  title text not null,
  accessed_at text not null default (datetime('now')),
  snapshot text not null default '',
  created_at text not null default (datetime('now')),
  updated_at text not null default (datetime('now')),
  check (source_type != 'web' or url is not null)
);

create unique index idx_writer_sources_course_document
  on writer_sources(class_id, document_id)
  where source_type = 'course' and document_id is not null;
create unique index idx_writer_sources_web_url
  on writer_sources(class_id, url)
  where source_type = 'web';
create index idx_writer_sources_class on writer_sources(class_id, source_type, id);

-- Excerpts are the small, auditable pieces the prose actually relied on. A section
-- reference is optional because some sources support the thesis or document as a whole.
create table writer_source_excerpts (
  id integer primary key autoincrement,
  source_id integer not null references writer_sources(id) on delete cascade,
  section_ref text,
  excerpt text not null check (length(trim(excerpt)) > 0),
  created_at text not null default (datetime('now'))
);

create index idx_writer_source_excerpts_source
  on writer_source_excerpts(source_id, id);
