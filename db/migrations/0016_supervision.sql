-- Supervision (TRZ-29, TRZ-32, TRZ-35): the audit sample draws, the customer's notifications and
-- the global automation switch.

-- Number of each audit sample draw (design 6.7). A sequence is not subject to row level
-- security, so the n-th draw is global although it runs in the session of one customer. A
-- transaction that rolls back still consumes its number.
CREATE SEQUENCE audit_draw_seq;
GRANT USAGE ON SEQUENCE audit_draw_seq TO trazo_app;

-- What the customer is told about the decisions on their clarifications, in the language of the
-- case. The text is written by code and passes the fact checker before it is stored; `checked`
-- keeps that result. `source_key` names the decision a notification comes from, so a replayed
-- decision does not notify twice.
CREATE TABLE notifications (
    id          bigserial PRIMARY KEY,
    customer_id text NOT NULL REFERENCES customers,
    case_id     text NOT NULL REFERENCES cases,
    kind        text NOT NULL
        CHECK (kind IN ('approved', 'rejected', 'info_requested', 'audit_reversed')),
    language    text NOT NULL CHECK (language IN ('es', 'pt')),
    text        text NOT NULL,
    checked     boolean NOT NULL,
    source_key  text NOT NULL UNIQUE,
    created_at  timestamp NOT NULL DEFAULT now(),
    read_at     timestamp
);
CREATE INDEX ix_notifications_customer ON notifications (customer_id);

ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE notifications FORCE ROW LEVEL SECURITY;
CREATE POLICY customer_own_rows ON notifications TO trazo_app
    USING (customer_id = current_setting('app.customer_id', true))
    WITH CHECK (customer_id = current_setting('app.customer_id', true));
CREATE POLICY analyst_all_rows ON notifications TO trazo_app
    USING (current_setting('app.role', true) = 'analyst')
    WITH CHECK (current_setting('app.role', true) = 'analyst');
GRANT SELECT, INSERT ON notifications TO trazo_app;
GRANT UPDATE (read_at) ON notifications TO trazo_app;
GRANT USAGE ON SEQUENCE notifications_id_seq TO trazo_app;

-- Global automation switch (design 11.5): one row, read on every decision, so turning it on
-- needs no redeploy. Every session reads it; only the analyst role changes it.
CREATE TABLE automation_switch (
    id           smallint PRIMARY KEY CHECK (id = 1),
    all_to_human boolean NOT NULL DEFAULT false,
    changed_by   text,
    changed_at   timestamp
);
INSERT INTO automation_switch (id, all_to_human) VALUES (1, false);

ALTER TABLE automation_switch ENABLE ROW LEVEL SECURITY;
ALTER TABLE automation_switch FORCE ROW LEVEL SECURITY;
CREATE POLICY everyone_reads ON automation_switch FOR SELECT TO trazo_app USING (true);
CREATE POLICY analyst_changes ON automation_switch FOR UPDATE TO trazo_app
    USING (current_setting('app.role', true) = 'analyst')
    WITH CHECK (current_setting('app.role', true) = 'analyst');
GRANT SELECT ON automation_switch TO trazo_app;
GRANT UPDATE (all_to_human, changed_by, changed_at) ON automation_switch TO trazo_app;
