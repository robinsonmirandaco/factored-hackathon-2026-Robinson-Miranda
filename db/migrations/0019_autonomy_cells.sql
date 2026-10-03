-- Autonomy of each intent x language cell (TRZ-30, design 6.7): the level in force and its open
-- block of reviews. The policy reads the level on every decision, in the session of a customer,
-- whose row level security hides the reviews of other customers in the audit log; so the state
-- lives here. Every session reads it; only the analyst role, whose decisions are the reviews,
-- writes it. A cell with no row is at the initial level of the policy. Since every session reads
-- it, a row keeps counts and no case of anyone: the reversed cases of a block are read from the
-- audit log, by the analyst, when the block closes.
CREATE TABLE autonomy_cells (
    intent          text NOT NULL,
    language        text NOT NULL CHECK (language IN ('es', 'pt')),
    level           text NOT NULL CHECK (level IN ('A0', 'A1', 'A2')),
    block_reviews   integer NOT NULL DEFAULT 0,
    block_reversals integer NOT NULL DEFAULT 0,
    -- Audit row of the last closed block of the cell: the open block holds the reviews after
    -- it. NULL while no block has closed.
    block_after     bigint,
    -- Consecutive closed blocks under the promotion threshold since the last change of level.
    good_blocks     integer NOT NULL DEFAULT 0,
    -- The block that set the level in force: r, W, N, threshold and its audit row; NULL while
    -- the cell has never changed.
    last_change     jsonb,
    updated_at      timestamp,
    PRIMARY KEY (intent, language),
    CHECK (block_reversals BETWEEN 0 AND block_reviews)
);

ALTER TABLE autonomy_cells ENABLE ROW LEVEL SECURITY;
ALTER TABLE autonomy_cells FORCE ROW LEVEL SECURITY;
CREATE POLICY everyone_reads ON autonomy_cells FOR SELECT TO trazo_app USING (true);
CREATE POLICY analyst_inserts ON autonomy_cells FOR INSERT TO trazo_app
    WITH CHECK (current_setting('app.role', true) = 'analyst');
CREATE POLICY analyst_changes ON autonomy_cells FOR UPDATE TO trazo_app
    USING (current_setting('app.role', true) = 'analyst')
    WITH CHECK (current_setting('app.role', true) = 'analyst');
GRANT SELECT, INSERT, UPDATE ON autonomy_cells TO trazo_app;
