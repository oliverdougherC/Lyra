-- The reviewer names the section it judged so a margin finding can queue a targeted
-- repair without trying to infer a section later from a quote that may have moved.
alter table draft_comments add column section_ref text;
