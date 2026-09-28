-- Authentication (TRZ-09): one-time codes by document, server-side sessions behind each JWT,
-- and the only lookup of a customer by document.

-- One row per document asking for a code, whether or not a customer has that document, so the
-- table never tells an existing document from an invented one. document_key is the HMAC of the
-- document (the same one customers keep); the code is stored only as a keyed hash too.
CREATE TABLE auth_challenges (
    document_key    text PRIMARY KEY,
    code_hash       text,
    expires_at      timestamp,
    failed_attempts integer NOT NULL DEFAULT 0,
    locked_until    timestamp,
    updated_at      timestamp NOT NULL DEFAULT now()
);

-- A JWT alone cannot expire after inactivity nor be revoked, so each token's jti has a row here.
-- Times are the real clock (design 5.1), never TRAZO_NOW. case_id is the last case the session
-- worked on: when the session ends, that case is expired and loses its pending action.
CREATE TABLE sessions (
    id           text PRIMARY KEY,
    subject      text NOT NULL,
    role         text NOT NULL CHECK (role IN ('customer', 'analyst')),
    case_id      text REFERENCES cases,
    created_at   timestamp NOT NULL,
    last_seen_at timestamp NOT NULL,
    expires_at   timestamp NOT NULL,
    ended_at     timestamp,
    ended_reason text CHECK (ended_reason IN ('idle', 'max_age', 'logout'))
);
CREATE INDEX ix_sessions_subject ON sessions (subject) WHERE ended_at IS NULL;

-- Neither table has row level security: both are read before the service knows the customer.
-- They hold no customer data beyond the session subject, and trazo_app cannot delete from them.
GRANT SELECT, INSERT, UPDATE ON auth_challenges, sessions TO trazo_app;

-- Before login no customer context exists, so RLS hides every customer row from trazo_app. The
-- lookup runs as trazo_auth instead: a role that cannot log in, reads only two columns of
-- customers and is not their owner, so RLS still applies to it through its own policy. The
-- function returns the id and nothing else. The document hash already covers the type.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trazo_auth') THEN
        CREATE ROLE trazo_auth NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;

GRANT SELECT (customer_id, document_hash) ON customers TO trazo_auth;
CREATE POLICY auth_lookup ON customers FOR SELECT TO trazo_auth USING (true);

-- search_path is pinned to the schema being migrated (public in every deployment; a throwaway
-- schema in tests) and pg_temp last, so no caller can shadow customers with its own table.
DO $$
BEGIN
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO trazo_auth', current_schema());
    EXECUTE format(
        'CREATE FUNCTION auth_customer_id(p_document_hash text) '
        'RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = %I, pg_temp AS '
        '$f$ SELECT customer_id FROM customers WHERE document_hash = p_document_hash $f$',
        current_schema()
    );
    EXECUTE format('GRANT trazo_auth TO %I', current_user);
END
$$;

ALTER FUNCTION auth_customer_id(text) OWNER TO trazo_auth;
REVOKE ALL ON FUNCTION auth_customer_id(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_customer_id(text) TO trazo_app;

-- A lockout of a document with no customer has no customer to file it under. These policies let
-- the authentication step, and only it, append such a row. The insert returns the new id, and
-- RETURNING needs the row to be readable too, hence the SELECT policy with the same condition.
CREATE POLICY auth_events ON audit_log FOR INSERT TO trazo_app
    WITH CHECK (
        current_setting('app.role', true) = 'auth' AND actor = 'auth' AND customer_id IS NULL
    );
CREATE POLICY auth_events_read ON audit_log FOR SELECT TO trazo_app
    USING (
        current_setting('app.role', true) = 'auth' AND actor = 'auth' AND customer_id IS NULL
    );
