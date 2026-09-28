-- Conversational context of the recognition step (TRZ-16). The policy now decides on the turn
-- after the customer sees the charge, so what the first message said about the card has to
-- outlive it. The options shown are kept so that a choice is accepted only when it names one
-- of them. Cases opened before this migration keep null in both.
ALTER TABLE cases
    ADD COLUMN card_in_possession boolean,
    ADD COLUMN shown_options      text[];
