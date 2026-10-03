-- Retention of redacted conversations (TRZ-41, design 11.4): 90 days, while the audit log, the
-- record of every decision, is kept. The conversation has no table of its own: its text lives in
-- audit rows and in info_requests. The purge replaces that text, and only that text, with a
-- fixed marker; every row stays, with its actor, action, rule, versions, ids, outcome, latency,
-- tokens and cost.

-- The conversation text of an audit payload or result, replaced by the marker at any depth:
-- the customer's message and its literal fragments, the reply, the translation, the analyst's
-- question and note, the merchant as the customer wrote it and the fragments the fact checker
-- flagged. Closed codes, ids, numbers and dates stay. Empty strings stay as they are.
CREATE FUNCTION retention_strip(j jsonb, parent text DEFAULT NULL) RETURNS jsonb
    LANGUAGE plpgsql IMMUTABLE
AS $$
DECLARE
    k text;
    v jsonb;
    out jsonb;
BEGIN
    IF j IS NULL THEN
        RETURN NULL;
    END IF;
    IF jsonb_typeof(j) = 'object' THEN
        out := '{}'::jsonb;
        FOR k, v IN SELECT * FROM jsonb_each(j) LOOP
            IF jsonb_typeof(v) = 'string' AND v <> '""'::jsonb AND (
                k IN ('redacted_text', 'evidence', 'expression', 'reply', 'note', 'question', 'text')
                OR (k = 'value' AND parent IN ('merchant_hint', 'unsupported'))
            ) THEN
                out := out || jsonb_build_object(k, '[eliminado por retención]');
            ELSE
                out := out || jsonb_build_object(k, retention_strip(v, k));
            END IF;
        END LOOP;
        RETURN out;
    END IF;
    IF jsonb_typeof(j) = 'array' THEN
        RETURN (
            SELECT coalesce(jsonb_agg(retention_strip(e, parent) ORDER BY i), '[]'::jsonb)
            FROM jsonb_array_elements(j) WITH ORDINALITY AS a(e, i)
        );
    END IF;
    RETURN j;
END
$$;

-- The audit log stays append-only. The one change allowed is the purge itself: inside
-- purge_conversation_text, a row may get its payload and result through retention_strip, with
-- every other column unchanged. Any other UPDATE, and every DELETE, still fails, for the owner
-- too.
CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE'
        AND current_setting('app.retention_purge', true) = 'on'
        AND (to_jsonb(NEW) - 'payload' - 'result') = (to_jsonb(OLD) - 'payload' - 'result')
        AND NEW.payload IS NOT DISTINCT FROM retention_strip(OLD.payload)
        AND NEW.result IS NOT DISTINCT FROM retention_strip(OLD.result)
    THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'audit_log is append-only: % is not allowed', TG_OP;
END
$$;

-- Replaces the conversation text written before the cutoff, in the audit log and in
-- info_requests, and says how many rows it cleaned and between which times they were written.
-- It runs as the owner (SECURITY DEFINER) because trazo_app may not update the audit log; it
-- checks the analyst role itself, and trazo_app is the only role that may call it. A second
-- call finds nothing left to clean.
CREATE FUNCTION purge_conversation_text(cutoff timestamp)
    RETURNS TABLE (audit_rows bigint, request_rows bigint, oldest_at timestamp, newest_at timestamp)
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path FROM CURRENT
AS $$
DECLARE
    marker CONSTANT text := '[eliminado por retención]';
    a_oldest timestamp;
    a_newest timestamp;
    r_oldest timestamp;
    r_newest timestamp;
BEGIN
    IF current_setting('app.role', true) IS DISTINCT FROM 'analyst' THEN
        RAISE EXCEPTION 'purge_conversation_text needs the analyst role'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('purge_conversation_text'));
    PERFORM set_config('app.retention_purge', 'on', true);
    WITH cleaned AS (
        UPDATE audit_log
        SET payload = retention_strip(payload), result = retention_strip(result)
        WHERE created_at < cutoff
            AND (retention_strip(payload) IS DISTINCT FROM payload
                 OR retention_strip(result) IS DISTINCT FROM result)
        RETURNING created_at
    )
    SELECT count(*), min(created_at), max(created_at) INTO audit_rows, a_oldest, a_newest
    FROM cleaned;
    PERFORM set_config('app.retention_purge', 'off', true);
    WITH cleaned AS (
        UPDATE info_requests
        SET question = marker, answer = CASE WHEN answer IS NULL THEN NULL ELSE marker END
        WHERE created_at < cutoff
            AND (question <> marker OR answer IS DISTINCT FROM marker AND answer IS NOT NULL)
        RETURNING created_at
    )
    SELECT count(*), min(created_at), max(created_at) INTO request_rows, r_oldest, r_newest
    FROM cleaned;
    oldest_at := least(a_oldest, r_oldest);
    newest_at := greatest(a_newest, r_newest);
    RETURN NEXT;
END
$$;
REVOKE ALL ON FUNCTION purge_conversation_text(timestamp) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_conversation_text(timestamp) TO trazo_app;
