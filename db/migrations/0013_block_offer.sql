-- An offered card block is its own action (TRZ-18): after a registration that offers the block,
-- the block waits for its own confirmation, and the customer may decline it.
ALTER TABLE case_actions DROP CONSTRAINT case_actions_action_check;
ALTER TABLE case_actions ADD CONSTRAINT case_actions_action_check
    CHECK (action IN ('register', 'register_and_offer_block', 'register_and_block', 'block'));
