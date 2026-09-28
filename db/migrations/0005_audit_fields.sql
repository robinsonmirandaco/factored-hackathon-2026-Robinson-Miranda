-- Every step records what it cost and which versions decided it. The columns accept null
-- because rows written before this migration cannot be filled in (the audit log is
-- append-only) and because a step that makes no LLM call has no model, prompt or tokens.
-- `verified` stays null until the post-action read-back exists (TRZ-19).
ALTER TABLE audit_log
    ADD COLUMN verified       boolean,
    ADD COLUMN input_tokens   integer,
    ADD COLUMN output_tokens  integer,
    ADD COLUMN cost_usd       numeric(12, 6),
    ADD COLUMN model          text,
    ADD COLUMN prompt_version text,
    ADD COLUMN policy_version text;

-- '-' is the trace_id of code running outside a request; a row carrying it could never be
-- joined back to the request that wrote it.
ALTER TABLE audit_log ADD CONSTRAINT ck_audit_log_trace_id CHECK (trace_id <> '-');

-- CREATE OR REPLACE may only append columns, and it keeps the grant of 0003 on the view.
CREATE OR REPLACE VIEW case_history WITH (security_invoker = true) AS
SELECT id, case_id, customer_id, trace_id, actor, action, result, latency_ms, created_at,
       payload, verified, input_tokens, output_tokens, cost_usd, model, prompt_version,
       policy_version
FROM audit_log
WHERE case_id IS NOT NULL;
