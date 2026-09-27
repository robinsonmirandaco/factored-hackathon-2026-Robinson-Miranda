-- Serving schema (TRZ-07). Bank data comes from the gold cohort with its lineage columns;
-- the operational tables are written by the service.
-- Bank timestamps are naive local time of the customer's country, like TRAZO_NOW, so the
-- simulated clock compares them without conversion. Operational timestamps are the server clock.

CREATE TABLE customers (
    customer_id      text PRIMARY KEY,
    document_type    text NOT NULL,
    -- HMAC-SHA256 of type and number under DOCUMENT_HASH_KEY; the number is never stored.
    document_hash    text NOT NULL UNIQUE,
    first_name       text,
    country          text NOT NULL,
    country_code     text NOT NULL,
    timezone         text NOT NULL,
    segment          text NOT NULL,
    customer_status  text NOT NULL,
    source_file      text,
    partition_date   date,
    batch_id         text,
    ingested_at      timestamp,
    pipeline_version text
);

CREATE TABLE products (
    product_id           text PRIMARY KEY,
    customer_id          text NOT NULL REFERENCES customers,
    product_type         text NOT NULL,
    product_number_last4 text,
    currency             text NOT NULL,
    current_balance      numeric(18, 2),
    credit_limit         numeric(18, 2),
    opening_date         date,
    expiration_date      date,
    product_status       text NOT NULL,
    source_file          text,
    partition_date       date,
    batch_id             text,
    ingested_at          timestamp,
    pipeline_version     text,
    -- Target of the ownership foreign keys below: a row can only point at a product of its
    -- own customer.
    UNIQUE (product_id, customer_id)
);
CREATE INDEX ix_products_customer ON products (customer_id);

CREATE TABLE transactions (
    transaction_id         text PRIMARY KEY,
    transaction_date       timestamp NOT NULL,
    process_date           date NOT NULL,
    product_id             text NOT NULL,
    customer_id            text NOT NULL REFERENCES customers,
    transaction_type       text NOT NULL,
    transaction_category   text,
    amount                 numeric(18, 2) NOT NULL,
    currency               text NOT NULL,
    amount_usd             numeric(18, 2),
    channel                text NOT NULL,
    branch_id              text,
    merchant_name          text,
    merchant_category      text,
    transaction_country    text,
    transaction_city       text,
    transaction_status     text NOT NULL,
    response_code          text,
    country_code           text,
    timezone               text,
    event_date_local       date,
    alert_currency_country boolean,
    source_file            text,
    partition_date         date,
    batch_id               text,
    ingested_at            timestamp,
    pipeline_version       text,
    UNIQUE (transaction_id, customer_id),
    FOREIGN KEY (product_id, customer_id) REFERENCES products (product_id, customer_id)
);
CREATE INDEX ix_transactions_customer_date ON transactions (customer_id, transaction_date);

CREATE TABLE complaints (
    complaint_id                   text PRIMARY KEY,
    customer_id                    text NOT NULL REFERENCES customers,
    creation_date                  timestamp NOT NULL,
    process_date                   date NOT NULL,
    case_type                      text NOT NULL,
    category                       text NOT NULL,
    subcategory                    text,
    reception_channel              text NOT NULL,
    has_affected_product           boolean NOT NULL,
    claimed_amount                 numeric(18, 2),
    currency                       text,
    priority                       text NOT NULL,
    status                         text NOT NULL,
    assignment_date                timestamp,
    first_response_date            timestamp,
    resolution_date                timestamp,
    closing_date                   timestamp,
    sla_breached                   boolean NOT NULL,
    resolution_days                double precision,
    is_repeat_complainer           boolean NOT NULL,
    country_code                   text,
    timezone                       text,
    event_date_local               date,
    alert_amount_currency_mismatch boolean,
    source_file                    text,
    partition_date                 date,
    batch_id                       text,
    ingested_at                    timestamp,
    pipeline_version               text
);
CREATE INDEX ix_complaints_customer_date ON complaints (customer_id, creation_date);

CREATE TABLE exchange_rates (
    date                         date NOT NULL,
    source_currency              text NOT NULL,
    target_currency              text NOT NULL,
    exchange_rate                double precision NOT NULL,
    buy_rate                     double precision,
    sell_rate                    double precision,
    source                       text,
    alert_duplicate_business_key boolean,
    source_file                  text,
    partition_date               date,
    batch_id                     text,
    ingested_at                  timestamp,
    pipeline_version             text,
    PRIMARY KEY (date, source_currency, target_currency)
);

CREATE TABLE cases (
    id                 text PRIMARY KEY,
    customer_id        text NOT NULL REFERENCES customers,
    transaction_id     text REFERENCES transactions,
    intent             text NOT NULL,
    status             text NOT NULL DEFAULT 'open',
    autonomy_level     text NOT NULL DEFAULT 'L0',
    recommended_action text,
    escalation_reason  text,
    human_decision     text,
    summary            text,
    trace_id           text NOT NULL,
    created_at         timestamp NOT NULL DEFAULT now(),
    updated_at         timestamp NOT NULL DEFAULT now()
);
CREATE INDEX ix_cases_customer ON cases (customer_id);
CREATE INDEX ix_cases_status ON cases (status);
CREATE INDEX ix_cases_trace ON cases (trace_id);

CREATE TABLE case_queue (
    id          bigserial PRIMARY KEY,
    case_id     text NOT NULL REFERENCES cases,
    customer_id text NOT NULL REFERENCES customers,
    kind        text NOT NULL CHECK (kind IN ('escalation', 'audit_sample')),
    reason      text,
    created_at  timestamp NOT NULL DEFAULT now(),
    resolved_at timestamp,
    UNIQUE (case_id, kind)
);

CREATE TABLE disputes (
    id             bigserial PRIMARY KEY,
    -- DSP-AAAA-NNNNN, assigned from TRZ-18 on.
    folio          text UNIQUE,
    customer_id    text NOT NULL,
    case_id        text NOT NULL REFERENCES cases,
    transaction_id text NOT NULL,
    dispute_type   text NOT NULL,
    reason         text,
    amount         numeric(18, 2),
    currency       text,
    status         text NOT NULL DEFAULT 'opened',
    due_date       date,
    created_at     timestamp NOT NULL DEFAULT now(),
    FOREIGN KEY (transaction_id, customer_id) REFERENCES transactions (transaction_id, customer_id),
    UNIQUE (case_id, transaction_id)
);
CREATE INDEX ix_disputes_customer ON disputes (customer_id);

CREATE TABLE card_blocks (
    id            bigserial PRIMARY KEY,
    customer_id   text NOT NULL,
    product_id    text NOT NULL,
    case_id       text NOT NULL REFERENCES cases,
    reason        text NOT NULL,
    status_before text NOT NULL,
    created_at    timestamp NOT NULL DEFAULT now(),
    FOREIGN KEY (product_id, customer_id) REFERENCES products (product_id, customer_id),
    UNIQUE (case_id, product_id)
);
CREATE INDEX ix_card_blocks_customer ON card_blocks (customer_id);

-- No foreign keys: the audit log outlives any row it mentions.
CREATE TABLE audit_log (
    id              bigserial PRIMARY KEY,
    trace_id        text NOT NULL,
    case_id         text,
    customer_id     text,
    actor           text NOT NULL,
    action          text NOT NULL,
    idempotency_key text UNIQUE,
    payload         jsonb,
    result          jsonb,
    latency_ms      integer,
    created_at      timestamp NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_log_trace ON audit_log (trace_id);
CREATE INDEX ix_audit_log_case ON audit_log (case_id);
CREATE INDEX ix_audit_log_customer ON audit_log (customer_id);
CREATE INDEX ix_audit_log_created ON audit_log (created_at);

-- The analyst's history of a case. security_invoker makes the view run with the caller's
-- rights, so the RLS of audit_log applies; by default a view runs as its owner and skips RLS.
CREATE VIEW case_history WITH (security_invoker = true) AS
SELECT id, case_id, customer_id, trace_id, actor, action, result, latency_ms, created_at
FROM audit_log
WHERE case_id IS NOT NULL;

CREATE TABLE quarantine (
    id         bigserial PRIMARY KEY,
    source     text NOT NULL,
    table_name text NOT NULL,
    reason     text NOT NULL,
    raw        jsonb NOT NULL,
    created_at timestamp NOT NULL DEFAULT now()
);
CREATE INDEX ix_quarantine_source ON quarantine (source);

-- One row per seed. A database holds a single data source (cohort or synthetic); the seed
-- command reads this table to refuse mixing them.
CREATE TABLE seed_runs (
    id        bigserial PRIMARY KEY,
    source    text NOT NULL CHECK (source IN ('cohort', 'synthetic')),
    detail    jsonb NOT NULL,
    loaded_at timestamp NOT NULL DEFAULT now()
);
