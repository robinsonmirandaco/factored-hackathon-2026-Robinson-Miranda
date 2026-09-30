-- Analyst decisions and the customer's answer to a request for information (TRZ-27, TRZ-28).
--
-- A case goes back to the queue when the customer answers, so it can have several queue rows
-- over time: the unique pair becomes one open row per case and kind. `updated` marks the row of
-- a case that came back with the customer's answer.
ALTER TABLE case_queue DROP CONSTRAINT case_queue_case_id_kind_key;
CREATE UNIQUE INDEX ux_case_queue_one_open ON case_queue (case_id, kind)
    WHERE resolved_at IS NULL;
ALTER TABLE case_queue ADD COLUMN updated boolean NOT NULL DEFAULT false;

-- What the analyst asked and what the customer answered, both PII-redacted before they are
-- stored. The deadline is counted in business days of the customer's country on the simulated
-- clock (design 5.1); `status_before` is the status the case goes back to with the answer.
CREATE TABLE info_requests (
    id            bigserial PRIMARY KEY,
    case_id       text NOT NULL REFERENCES cases,
    customer_id   text NOT NULL REFERENCES customers,
    question      text NOT NULL,
    asked_by      text NOT NULL,
    asked_on      date NOT NULL,
    due_on        date NOT NULL,
    status_before text NOT NULL,
    answer        text,
    answered_at   timestamp,
    status        text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered', 'expired')),
    created_at    timestamp NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_info_requests_one_open ON info_requests (case_id) WHERE status = 'open';
CREATE INDEX ix_info_requests_customer ON info_requests (customer_id);

ALTER TABLE info_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE info_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY customer_own_rows ON info_requests TO trazo_app
    USING (customer_id = current_setting('app.customer_id', true))
    WITH CHECK (customer_id = current_setting('app.customer_id', true));
CREATE POLICY analyst_all_rows ON info_requests TO trazo_app
    USING (current_setting('app.role', true) = 'analyst')
    WITH CHECK (current_setting('app.role', true) = 'analyst');
GRANT SELECT, INSERT ON info_requests TO trazo_app;
GRANT UPDATE (answer, answered_at, status) ON info_requests TO trazo_app;
GRANT USAGE ON SEQUENCE info_requests_id_seq TO trazo_app;
