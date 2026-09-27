-- The audit log is the record of every decision, so no row may change or disappear. trazo_app
-- has no UPDATE or DELETE grant; the trigger also stops the owner. TRUNCATE stays possible for
-- the owner only, because `make seed REPLACE=1` empties the whole database before a new source.
CREATE FUNCTION audit_log_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only: % is not allowed', TG_OP;
END
$$;

CREATE TRIGGER audit_log_append_only
BEFORE UPDATE OR DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();
