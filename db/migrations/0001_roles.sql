-- Application role. The API connects only as trazo_app: it owns no table, is not a superuser
-- and has no BYPASSRLS, so row level security always applies to it. Roles belong to the whole
-- cluster, so the role is created once and every schema only grants it access.
-- The password is set by the migration runner from DATABASE_URL; SQL cannot read the environment.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trazo_app') THEN
        CREATE ROLE trazo_app NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;

DO $$
BEGIN
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO trazo_app', current_schema());
END
$$;
