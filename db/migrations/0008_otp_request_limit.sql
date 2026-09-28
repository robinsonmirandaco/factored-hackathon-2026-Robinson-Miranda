-- Requests for a one-time code, counted per document in a fixed window (TRZ-09). Counted for
-- invented documents too, so the limit answers the same whether a customer has the document.
ALTER TABLE auth_challenges
    ADD COLUMN requests_since timestamp,
    ADD COLUMN request_count  integer NOT NULL DEFAULT 0;
