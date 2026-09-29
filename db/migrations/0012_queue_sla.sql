-- Escalation queue with priority and SLA, and the clarifications of a case (TRZ-25). Every case
-- handed to a person gets one queue row with the priority the policy gave it and the moment its
-- SLA ends, on the real clock (operation, not bank data). Rows written before this migration
-- keep priority normal and no SLA. The clarifications counter caps the questions a case asks
-- before it goes to a person (design 5.1).
ALTER TABLE case_queue
    ADD COLUMN priority   text NOT NULL DEFAULT 'normal'
        CHECK (priority IN ('normal', 'high', 'urgent')),
    ADD COLUMN sla_due_at timestamp;

ALTER TABLE cases
    ADD COLUMN clarifications integer NOT NULL DEFAULT 0 CHECK (clarifications >= 0);
