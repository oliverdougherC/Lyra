-- Credentials are immutable slots; selecting one commits with the endpoint/model.
-- Legacy secrets remain bound to the endpoint that existed at migration time.
alter table settings add column tutor_credential_id text;
alter table settings add column legacy_credential_endpoint text;
update settings set legacy_credential_endpoint = endpoint_url where id = 1;

create trigger settings_probe_credential_changed
 after update of tutor_credential_id on settings
 when old.tutor_credential_id is not new.tutor_credential_id
begin
    update settings set probe_revision = probe_revision + 1,
        tools_supported = null, tools_message = null,
        vision_supported = null, vision_message = null
    where id = new.id;
end;
