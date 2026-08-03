create table classes (
  id integer primary key autoincrement,
  name text not null,
  code text,
  semester text,
  created_at text not null default (datetime('now')),
  last_active_at text not null default (datetime('now'))
);

create table documents (
  id integer primary key autoincrement,
  class_id integer not null references classes(id) on delete cascade,
  filename text not null,
  stored_path text not null,
  mime text not null,
  byte_size integer not null,
  state text not null check (state in
    ('pending','parsing','chunking','embedding','extracting','ready','failed','unsupported')),
  stage_detail text,
  pages_total integer,
  pages_done integer not null default 0,
  pages_skipped integer not null default 0,
  error_message text,
  created_at text not null default (datetime('now'))
);
create index idx_documents_class on documents(class_id);

create table chunks (
  id integer primary key autoincrement,
  document_id integer not null references documents(id) on delete cascade,
  class_id integer not null references classes(id) on delete cascade,
  content text not null,
  token_count integer not null,
  page_number integer,
  section_title text,
  problem_number text,
  part_index integer,
  doc_type text not null,
  embedding_model text not null,
  embedding_dim integer not null
);
create index idx_chunks_document on chunks(document_id);
create index idx_chunks_class on chunks(class_id);

create table chat_sessions (
  id integer primary key autoincrement,
  class_id integer not null references classes(id) on delete cascade,
  title text,
  mode text not null default 'guide' check (mode in ('guide','show')),
  created_at text not null default (datetime('now'))
);

create table messages (
  id integer primary key autoincrement,
  session_id integer not null references chat_sessions(id) on delete cascade,
  role text not null check (role in ('user','assistant')),
  content text not null,
  retrieval_trimmed integer not null default 0,
  omitted_document_count integer not null default 0,
  created_at text not null default (datetime('now'))
);
create index idx_messages_session on messages(session_id);

create table profile_facts (
  id integer primary key autoincrement,
  class_id integer references classes(id) on delete cascade,
  kind text not null check (kind in
    ('deadline','topic','grading','professor','prerequisite','note')),
  label text not null,
  value text not null,
  confidence text not null check (confidence in ('high','low')),
  confirmed integer not null default 0,
  rejected integer not null default 0,
  source_document_id integer references documents(id) on delete set null,
  created_at text not null default (datetime('now'))
);
create index idx_facts_class on profile_facts(class_id);

create table settings (
  id integer primary key check (id = 1),
  endpoint_url text,
  model text,
  context_window integer not null default 8192,
  extraction_enabled integer not null default 1,
  remote_ack integer not null default 0,
  -- no theme column: theme is client-only, in localStorage key `lyra-theme`
  embedding_model text,
  embedding_dim integer
);
insert into settings (id) values (1);

create virtual table chunk_embeddings using vec0(
  chunk_id integer primary key,
  class_id integer partition key,
  embedding float[768] distance_metric=cosine
);
