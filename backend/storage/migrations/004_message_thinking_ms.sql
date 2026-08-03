-- How long the model spent thinking before its first word of answer. Stored rather than
-- recomputed, because the interface reports it on a reopened conversation too, and the only
-- moment that duration can be measured is while the turn is streaming. Zero means either a
-- model that does not think or a turn from before this column existed.
alter table messages add column thinking_ms integer not null default 0;
