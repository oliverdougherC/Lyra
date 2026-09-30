-- A student's display name is metadata. The original filename and source identity remain intact.
alter table documents add column nickname text;
