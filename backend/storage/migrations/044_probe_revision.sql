-- Non-secret measurement epoch. The trigger covers every endpoint/model writer,
-- including callers outside the settings route, in the same committing statement.
alter table settings add column probe_revision integer not null default 0;

create trigger settings_probe_configuration_changed
 after update of endpoint_url, model on settings
 when old.endpoint_url is not new.endpoint_url or old.model is not new.model
begin
    update settings set probe_revision = probe_revision + 1,
        tools_supported = null, tools_message = null,
        vision_supported = null, vision_message = null
    where id = new.id;
end;
