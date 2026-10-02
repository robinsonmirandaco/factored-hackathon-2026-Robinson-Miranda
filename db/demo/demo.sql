-- Demo objects (TRZ-38). Not a migration: `make seed-demo` runs this file as the schema owner,
-- so only a database prepared for the demo has them, and a database without them cannot be
-- reset. Running it again replaces the function and keeps both tables.

-- The customers the demo seed chose for each role and the charge each one uses. The rule that
-- chose them lives in config/demo.yaml; the ids live only here, in the database.
CREATE TABLE IF NOT EXISTS demo_roles (
    role           text NOT NULL,
    position       integer NOT NULL,
    customer_id    text NOT NULL REFERENCES customers,
    transaction_id text REFERENCES transactions,
    PRIMARY KEY (role, position)
);
ALTER TABLE demo_roles ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo_roles FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS analyst_reads ON demo_roles;
CREATE POLICY analyst_reads ON demo_roles FOR SELECT TO trazo_app
    USING (current_setting('app.role', true) = 'analyst');
GRANT SELECT ON demo_roles TO trazo_app;

-- One row per reset request, keyed by its Idempotency-Key: the same key returns the stored
-- answer and resets nothing. The reset never empties this table.
CREATE TABLE IF NOT EXISTS demo_resets (
    idempotency_key text PRIMARY KEY,
    analyst         text NOT NULL,
    response        jsonb NOT NULL,
    created_at      timestamp NOT NULL DEFAULT now()
);
ALTER TABLE demo_resets ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo_resets FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS analyst_rows ON demo_resets;
CREATE POLICY analyst_rows ON demo_resets FOR ALL TO trazo_app
    USING (current_setting('app.role', true) = 'analyst')
    WITH CHECK (current_setting('app.role', true) = 'analyst');
GRANT SELECT, INSERT ON demo_resets TO trazo_app;

-- Empties the operational tables and restarts the counters, keeping the bank's records and the
-- demo documents. It runs as the owner (SECURITY DEFINER) because trazo_app may not delete or
-- truncate anything; it checks the analyst role itself, and trazo_app is the only role that
-- may call it. The audit log is emptied too: the autonomy cells count their reviews from it,
-- so keeping it would add every rehearsal's reversals to the next one. The append-only trigger
-- still stops any UPDATE or DELETE of its rows.
CREATE OR REPLACE FUNCTION demo_reset() RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path FROM CURRENT
AS $$
BEGIN
    IF current_setting('app.role', true) IS DISTINCT FROM 'analyst' THEN
        RAISE EXCEPTION 'demo_reset needs the analyst role' USING ERRCODE = 'insufficient_privilege';
    END IF;
    -- One reset at a time; the service takes the same lock before it reads the key.
    PERFORM pg_advisory_xact_lock(hashtext('demo_reset'));

    -- A card blocked during the demo goes back to the status it had before its first block.
    UPDATE products p SET product_status = b.status_before
    FROM (
        SELECT DISTINCT ON (product_id) product_id, status_before
        FROM card_blocks ORDER BY product_id, id
    ) b
    WHERE p.product_id = b.product_id;

    -- Sessions point at cases and hold the analyst's own session, so they are not emptied: the
    -- customers' sessions end, and the analyst who asked for the reset stays signed in.
    UPDATE sessions SET case_id = NULL WHERE case_id IS NOT NULL;
    UPDATE sessions SET ended_at = now() AT TIME ZONE 'utc', ended_reason = 'logout'
    WHERE role = 'customer' AND ended_at IS NULL;

    DELETE FROM notifications;
    DELETE FROM info_requests;
    DELETE FROM case_actions;
    DELETE FROM card_blocks;
    DELETE FROM disputes;
    DELETE FROM case_queue;
    DELETE FROM cases;
    TRUNCATE audit_log;
    DELETE FROM auth_challenges;

    ALTER SEQUENCE audit_draw_seq RESTART;
    ALTER SEQUENCE dispute_folio_seq RESTART;
    UPDATE automation_switch SET all_to_human = false, changed_by = NULL, changed_at = NULL;
END
$$;
REVOKE ALL ON FUNCTION demo_reset() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION demo_reset() TO trazo_app;
