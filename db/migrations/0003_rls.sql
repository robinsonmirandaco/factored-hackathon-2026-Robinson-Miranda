-- Row level security (TRZ-07). Every table that holds a customer's rows gets two policies for
-- trazo_app: the customer of the transaction sees and writes only their own rows, and the
-- analyst role sees and writes all of them. Both read transaction-local settings that the
-- service fixes with set_config(..., true) at the start of every transaction.
-- With no setting, current_setting(..., true) is NULL and no row matches: access fails closed.
-- FORCE applies the policies to the table owner too; a superuser still bypasses them, which is
-- why the service never connects as one (the API refuses to start if it does).
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'customers', 'products', 'transactions', 'complaints', 'cases', 'case_queue',
        'disputes', 'card_blocks', 'audit_log'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY customer_own_rows ON %I TO trazo_app '
            'USING (customer_id = current_setting(''app.customer_id'', true)) '
            'WITH CHECK (customer_id = current_setting(''app.customer_id'', true))',
            t
        );
        EXECUTE format(
            'CREATE POLICY analyst_all_rows ON %I TO trazo_app '
            'USING (current_setting(''app.role'', true) = ''analyst'') '
            'WITH CHECK (current_setting(''app.role'', true) = ''analyst'')',
            t
        );
    END LOOP;
END
$$;

-- Grants cover what the service does today; nothing else is reachable. Reference data has no
-- customer and no RLS; quarantine is write-only; seed_runs and schema_migrations are admin-only.
GRANT SELECT ON customers, products, transactions, complaints, exchange_rates, cases,
    case_queue, disputes, card_blocks, audit_log, case_history TO trazo_app;
GRANT INSERT, UPDATE ON cases, case_queue TO trazo_app;
GRANT UPDATE (product_status) ON products TO trazo_app;
GRANT INSERT ON disputes, card_blocks, audit_log, quarantine TO trazo_app;
GRANT USAGE ON SEQUENCE case_queue_id_seq, disputes_id_seq, card_blocks_id_seq,
    audit_log_id_seq, quarantine_id_seq TO trazo_app;
