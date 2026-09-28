-- Registering a dispute and blocking the card of the charge (TRZ-18).

-- The business date of a dispute: the simulated "now" of the case when it was registered. The
-- open-dispute rule counts own disputes on this clock and window, like the complaints of the
-- dataset; created_at stays the server clock. Rows registered before this migration keep null
-- and no longer count.
ALTER TABLE disputes ADD COLUMN business_at timestamp;

-- Folio DSP-AAAA-NNNNN: the number part. MAXVALUE fails loudly instead of widening the format.
-- It does not restart each year and may leave gaps when a transaction rolls back.
CREATE SEQUENCE dispute_folio_seq MINVALUE 1 MAXVALUE 99999 NO CYCLE;
GRANT USAGE ON SEQUENCE dispute_folio_seq TO trazo_app;

-- Every action offered for confirmation. A confirmation names one of these ids, so a "sí" given
-- to an action that was replaced, executed or cancelled can never run a different one.
CREATE TABLE case_actions (
    id             text PRIMARY KEY,
    case_id        text NOT NULL REFERENCES cases,
    customer_id    text NOT NULL,
    action         text NOT NULL
        CHECK (action IN ('register', 'register_and_offer_block', 'register_and_block')),
    transaction_id text NOT NULL,
    status         text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'replaced', 'executed', 'canceled')),
    created_at     timestamp NOT NULL DEFAULT now(),
    resolved_at    timestamp,
    FOREIGN KEY (transaction_id, customer_id) REFERENCES transactions (transaction_id, customer_id)
);
CREATE UNIQUE INDEX ux_case_actions_one_pending ON case_actions (case_id) WHERE status = 'pending';
CREATE INDEX ix_case_actions_customer ON case_actions (customer_id);

ALTER TABLE case_actions ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_actions FORCE ROW LEVEL SECURITY;
CREATE POLICY customer_own_rows ON case_actions TO trazo_app
    USING (customer_id = current_setting('app.customer_id', true))
    WITH CHECK (customer_id = current_setting('app.customer_id', true));
CREATE POLICY analyst_all_rows ON case_actions TO trazo_app
    USING (current_setting('app.role', true) = 'analyst')
    WITH CHECK (current_setting('app.role', true) = 'analyst');
GRANT SELECT, INSERT ON case_actions TO trazo_app;
GRANT UPDATE (status, resolved_at) ON case_actions TO trazo_app;
