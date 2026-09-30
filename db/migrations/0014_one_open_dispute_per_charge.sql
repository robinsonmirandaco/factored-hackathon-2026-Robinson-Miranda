-- One opened dispute per charge (TRZ-18): two cases on the same transaction could each register
-- one, since the idempotency key of register_dispute is per case. The tool refuses a second one;
-- this index holds the rule in the database too.
CREATE UNIQUE INDEX ux_disputes_one_open_per_transaction ON disputes (transaction_id)
    WHERE status = 'opened';
