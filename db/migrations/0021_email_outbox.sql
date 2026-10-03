-- Transactional email behind a flag (TRZ-33). The email of a notification is written here in the
-- same transaction as the notification, and a scheduled process sends it with retries (outbox
-- pattern), so a provider that is down loses nothing. The body is the notification text, already
-- checked; the address is the test inbox of the settings, never one of the dataset. Deleting a
-- notification, as the demo reset does, deletes its email.
CREATE TABLE email_outbox (
    id              bigserial PRIMARY KEY,
    notification_id bigint NOT NULL UNIQUE REFERENCES notifications ON DELETE CASCADE,
    case_id         text NOT NULL,
    customer_id     text NOT NULL,
    to_address      text NOT NULL,
    subject         text NOT NULL,
    body            text NOT NULL,
    status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    attempts        integer NOT NULL DEFAULT 0,
    next_attempt_at timestamp NOT NULL DEFAULT now(),
    last_error      text,
    sent_at         timestamp,
    created_at      timestamp NOT NULL DEFAULT now()
);
CREATE INDEX ix_email_outbox_due ON email_outbox (next_attempt_at) WHERE status = 'pending';

-- Only the analyst role writes notifications and sends their email; no customer session reads
-- this table.
ALTER TABLE email_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY analyst_all_rows ON email_outbox TO trazo_app
    USING (current_setting('app.role', true) = 'analyst')
    WITH CHECK (current_setting('app.role', true) = 'analyst');
GRANT SELECT, INSERT ON email_outbox TO trazo_app;
GRANT UPDATE (status, attempts, next_attempt_at, last_error, sent_at) ON email_outbox TO trazo_app;
GRANT USAGE ON SEQUENCE email_outbox_id_seq TO trazo_app;
