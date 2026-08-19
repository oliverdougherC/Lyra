-- Durable classification for a storage intent whose recorded work is unsafe to perform:
-- an unreadable payload, a recorded path outside the current data tree, an unknown kind.
--
-- Null means the intent is actionable (settled on completion, retried after a transient
-- failure). Non-null keeps the intent - and its payload, the only durable pointer to the
-- work owed - visible for manual handling instead of silently settling it with the work
-- skipped, and distinguishes it from a transient failure worth retrying blindly. Startup
-- re-validates blocked intents (cheap, never destructive), so one blocked by a
-- temporarily relocated data directory settles by itself once the environment is back.

alter table storage_intents add column blocked_reason text;
